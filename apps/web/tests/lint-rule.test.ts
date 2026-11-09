import { describe, expect, it } from "vitest";
import { Linter } from "eslint";
import plugin from "../eslint-rules/plugin.mjs";
import { parser } from "typescript-eslint";

function lint(code: string): string[] {
  const linter = new Linter();
  const messages = linter.verify(code, [{
    files: ["**/*.tsx"],
    languageOptions: { parser, parserOptions: { ecmaFeatures: { jsx: true } } },
    plugins: { memorylab: plugin },
    rules: { "memorylab/no-raw-html": "error" },
  }], "file.tsx");
  return messages.map((m) => m.messageId ?? m.message);
}

describe("memorylab/no-raw-html fires on every way to make HTML from a string", () => {
  const bad: [string, string, string][] = [
    ["dangerouslySetInnerHTML attribute", "const a = <div dangerouslySetInnerHTML={{ __html: x }} />;", "jsx"],
    ["dangerouslySetInnerHTML in a props object", "const p = { dangerouslySetInnerHTML: { __html: x } };", "jsx"],
    ["innerHTML assignment", "el.innerHTML = x;", "prop"],
    ["innerHTML read", "const h = el.innerHTML;", "prop"],
    ["outerHTML", "el.outerHTML = x;", "prop"],
    ["insertAdjacentHTML", "el.insertAdjacentHTML('beforeend', x);", "prop"],
    ["bracket access", "el['innerHTML'] = x;", "prop"],
    ["document.write", "document.write(x);", "write"],
    ["srcDoc", "const f = <iframe srcDoc={x} />;", "set"],
    ["marked import", "import { marked } from 'marked';", "module"],
    ["react-markdown import", "import Markdown from 'react-markdown';", "module"],
    ["remark plugin import", "import remarkGfm from 'remark-gfm';", "module"],
    ["markdown-it require", "const md = require('markdown-it');", "module"],
    ["dynamic markdown import", "const m = await import('showdown');", "module"],
  ];
  for (const [name, code, id] of bad) {
    it(`flags ${name}`, () => expect(lint(code)).toContain(id));
  }
  it("accepts ordinary text rendering", () => {
    expect(lint("const a = <pre>{text}</pre>; el.textContent = x; import React from 'react';")).toEqual([]);
  });
});
