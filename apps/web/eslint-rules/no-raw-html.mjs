/** Model, memory and title text is attacker-influenced, so it is only ever rendered as text nodes. This rule bans every way of
 *  turning a string into HTML: dangerouslySetInnerHTML, innerHTML/outerHTML/insertAdjacentHTML, document.write, and markdown-to-HTML
 *  libraries. A test (tests/lint-rule.test.ts) checks that it fires. */

const BANNED_PROPERTIES = new Set(["innerHTML", "outerHTML", "insertAdjacentHTML", "srcdoc"]);
const BANNED_MODULES = /^(marked|markdown-it|react-markdown|remark(-.*)?|rehype(-.*)?|showdown|snarkdown|micromark|mdx|@mdx-js\/.*|commonmark|markdown-to-jsx)$/;

const rule = {
  meta: { type: "problem", schema: [], messages: {
    jsx: "dangerouslySetInnerHTML is banned: render text as a text node.",
    prop: "Assigning or reading `{{name}}` is banned: render text as a text node.",
    write: "document.write is banned.",
    module: "Importing `{{name}}` is banned: no markdown-to-HTML.",
    set: "Setting `{{name}}` is banned.",
  } },
  create(context) {
    const check = (node, name) => context.report({ node, messageId: "prop", data: { name } });
    return {
      JSXAttribute(node) {
        if (node.name?.name === "dangerouslySetInnerHTML") context.report({ node, messageId: "jsx" });
        if (node.name?.name === "srcDoc") context.report({ node, messageId: "set", data: { name: "srcDoc" } });
      },
      Identifier(node) {
        if (node.name === "dangerouslySetInnerHTML" && node.parent?.type !== "JSXAttribute") context.report({ node, messageId: "jsx" });
      },
      MemberExpression(node) {
        const name = node.property?.type === "Identifier" ? node.property.name : node.property?.type === "Literal" ? String(node.property.value) : "";
        if (BANNED_PROPERTIES.has(name)) check(node.property, name);
        if (name === "write" || name === "writeln") {
          if (node.object?.type === "Identifier" && node.object.name === "document") context.report({ node, messageId: "write" });
        }
      },
      Property(node) {
        const key = node.key?.type === "Identifier" ? node.key.name : node.key?.type === "Literal" ? String(node.key.value) : "";
        if (key === "dangerouslySetInnerHTML" || BANNED_PROPERTIES.has(key)) check(node.key, key);
      },
      ImportDeclaration(node) {
        if (BANNED_MODULES.test(String(node.source.value))) context.report({ node, messageId: "module", data: { name: node.source.value } });
      },
      CallExpression(node) {
        if (node.callee?.type === "Identifier" && node.callee.name === "require" && node.arguments[0]?.type === "Literal"
            && BANNED_MODULES.test(String(node.arguments[0].value))) context.report({ node, messageId: "module", data: { name: node.arguments[0].value } });
      },
      ImportExpression(node) {
        if (node.source?.type === "Literal" && BANNED_MODULES.test(String(node.source.value))) context.report({ node, messageId: "module", data: { name: node.source.value } });
      },
    };
  },
};

export default rule;
