from docutils import nodes
from docutils.parsers.rst import Directive, Parser as RstParser
from docutils.statemachine import StringList
from docutils.utils import new_document

from collections import OrderedDict

from sphinx import addnodes
from sphinx.util import rst
from sphinx.util.docutils import switch_source_input
from sphinx.ext.autosummary import autosummary_table, extract_summary

from sphinx_js.jsdoc import Analyzer as JsAnalyzer
from sphinx_js.ir import Function
from sphinx_js.parsers import path_and_formal_params, PathVisitor
from sphinx_js.renderers import AutoFunctionRenderer, AutoAttributeRenderer


class PyodideAnalyzer:
    """JsDoc automatically instantiates the JsAnalyzer. Rather than subclassing
    or monkey patching it, we use composition (see getattr impl).

    The main extra thing we do is reorganize the doclets based on our globals /
    functions / attributes scheme. This we use to subdivide the sections in our
    summary. We store these in the "js_docs" field which is the only field that
    we access later.
    """

    def __init__(self, analyzer: JsAnalyzer) -> None:
        self.inner = analyzer
        self.create_js_doclets()

    def __getattr__(self, key):
        return getattr(self.inner, key)

    def longname_to_path(self, name):
        """Convert the longname field produced by jsdoc to a path appropriate to use
        with _sphinxjs_analyzer.get_object. Based on:
        https://github.com/mozilla/sphinx-js/blob/3.1/sphinx_js/jsdoc.py#L181
        """
        return PathVisitor().visit(path_and_formal_params["path"].parse(name))

    def get_object_from_json(self, json):
        """Look up the JsDoc IR object corresponding to this object. We use the
        "kind" field to decide whether the object is a "function" or an
        "attribute". We use longname_to_path to convert the path into a list of
        path components which JsAnalyzer.get_object requires.
        """
        path = self.longname_to_path(json["longname"])
        kind = "function" if json["kind"] == "function" else "attribute"
        obj = self.inner.get_object(path, kind)
        obj.kind = kind
        return obj

    def create_js_doclets(self):
        """Search through the doclets generated by JsDoc and categorize them by
        summary section. Skip docs labeled as "@private".
        """

        def get_val():
            return OrderedDict([["attribute", []], ["function", []]])

        self.js_docs = {key: get_val() for key in ["globals", "pyodide", "PyProxy"]}
        items = {"PyProxy": []}
        for (key, group) in self._doclets_by_class.items():
            key = [x for x in key if "/" not in x]
            if key[-1] == "globalThis":
                items["globals"] = group
            if key[0] == "pyodide." and key[-1] == "Module":
                items["pyodide"] = group
            if key[0] == "pyproxy.":
                items["PyProxy"] += group

        for key, value in items.items():
            for json in value:
                if json.get("access", None) == "private":
                    continue
                obj = self.get_object_from_json(json)
                if obj.name[0] == '"' and obj.name[-1] == '"':
                    obj.name = "[" + obj.name[1:-1] + "]"
                self.js_docs[key][obj.kind].append(obj)


def get_jsdoc_content_directive(app):
    """These directives need to close over app """

    class JsDocContent(Directive):
        """A directive that just dumps a summary table in place. There are no
        options, it only prints the one thing, we control the behavior from
        here
        """

        required_arguments = 1

        def get_rst(self, obj):
            """Grab the appropriate renderer and render us to rst.
            JsDoc also has an AutoClassRenderer which may be useful in the future."""
            if isinstance(obj, Function):
                renderer = AutoFunctionRenderer
            else:
                renderer = AutoAttributeRenderer
            return renderer(self, app, arguments=["dummy"]).rst(
                [obj.name], obj, use_short_name=False
            )

        def get_rst_for_group(self, objects):
            return [self.get_rst(obj) for obj in objects]

        def parse_rst(self, rst):
            """We produce a bunch of rst but directives are supposed to output
            docutils trees. This is a helper that converts the rst to docutils.
            """
            settings = self.state.document.settings
            doc = new_document("", settings)
            RstParser().parse(rst, doc)
            return doc.children

        def run(self):
            module = self.arguments[0]
            values = app._sphinxjs_analyzer.js_docs[module]
            rst = []
            rst.append([f".. js:module:: {module}"])
            for group in values.values():
                rst.append(self.get_rst_for_group(group))
            joined_rst = "\n\n".join(["\n\n".join(r) for r in rst])
            return self.parse_rst(joined_rst)

    return JsDocContent


def get_jsdoc_summary_directive(app):
    class JsDocSummary(Directive):
        """A directive that just dumps the Js API docs in place. There are no
        options, it only prints the one thing, we control the behavior from
        here
        """

        required_arguments = 1

        def run(self):
            result = []
            module = self.arguments[0]
            value = app._sphinxjs_analyzer.js_docs[module]
            for group_name, group_objects in value.items():
                if not group_objects:
                    continue
                result.append(self.format_heading(group_name.title() + "s:"))
                table_items = self.get_summary_table(module, group_objects)
                table_markup = self.format_table(table_items)
                result.extend(table_markup)
            return result

        def format_heading(self, text):
            """Make a section heading. This corresponds to the rst: "**Heading:**"
            autodocsumm uses headings like that, so this will match that style.
            """
            heading = nodes.paragraph("")
            strong = nodes.strong("")
            strong.append(nodes.Text(text))
            heading.append(strong)
            return heading

        def extract_summary(self, descr):
            """Wrapper around autosummary extract_summary that is easier to use.
            It seems like colons need escaping for some reason.
            """
            colon_esc = "esccolon\\\xafhoa:"
            return extract_summary(
                [descr.replace(":", colon_esc)], self.state.document
            ).replace(colon_esc, ":")

        def get_sig(self, obj):
            """If the object is a function, get its signature (as figured by JsDoc)"""
            if isinstance(obj, Function):
                return AutoFunctionRenderer(
                    self, app, arguments=["dummy"]
                )._formal_params(obj)
            else:
                return ""

        def get_summary_row(self, pkgname, obj):
            """Get the summary table row for obj.

            The output is designed to be input to format_table. The link name
            needs to be set up so that :any:`link_name` makes a link to the
            actual api docs for this object.
            """
            sig = self.get_sig(obj)
            display_name = obj.name
            summary = self.extract_summary(obj.description)
            link_name = pkgname + "." + display_name
            return (display_name, sig, summary, link_name)

        def get_summary_table(self, pkgname, group):
            """Get the data for a summary table. Return value is set up to be an
            argument of format_table.
            """
            return [self.get_summary_row(pkgname, obj) for obj in group]

        # This following method is copied almost verbatim from autosummary
        # (where it is called get_table).
        #
        # We have to change the value of one string: qualifier = 'obj   ==>
        # qualifier = 'any'
        # https://github.com/sphinx-doc/sphinx/blob/3.x/sphinx/ext/autosummary/__init__.py#L392
        def format_table(self, items):
            """Generate a proper list of table nodes for autosummary:: directive.

            *items* is a list produced by :meth:`get_items`.
            """
            table_spec = addnodes.tabular_col_spec()
            table_spec["spec"] = r"\X{1}{2}\X{1}{2}"

            table = autosummary_table("")
            real_table = nodes.table("", classes=["longtable"])
            table.append(real_table)
            group = nodes.tgroup("", cols=2)
            real_table.append(group)
            group.append(nodes.colspec("", colwidth=10))
            group.append(nodes.colspec("", colwidth=90))
            body = nodes.tbody("")
            group.append(body)

            def append_row(*column_texts: str) -> None:
                row = nodes.row("")
                source, line = self.state_machine.get_source_and_line()
                for text in column_texts:
                    node = nodes.paragraph("")
                    vl = StringList()
                    vl.append(text, "%s:%d:<autosummary>" % (source, line))
                    with switch_source_input(self.state, vl):
                        self.state.nested_parse(vl, 0, node)
                        try:
                            if isinstance(node[0], nodes.paragraph):
                                node = node[0]
                        except IndexError:
                            pass
                        row.append(nodes.entry("", node))
                body.append(row)

            for name, sig, summary, real_name in items:
                qualifier = "any"  # <== Only thing changed from autosummary version
                if "nosignatures" not in self.options:
                    col1 = ":%s:`%s <%s>`\\ %s" % (
                        qualifier,
                        name,
                        real_name,
                        rst.escape(sig),
                    )
                else:
                    col1 = ":%s:`%s <%s>`" % (qualifier, name, real_name)
                col2 = summary
                append_row(col1, col2)

            return [table_spec, table]

    return JsDocSummary