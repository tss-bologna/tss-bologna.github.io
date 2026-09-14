"use strict";

/*
 * Build-time KaTeX renderer.
 *
 * Input:
 * [
 *   {"tex": "\\Gamma \\vdash t : A", "display": false},
 *   {"tex": "\\frac{a}{b}", "display": true}
 * ]
 *
 * Output:
 * ["<span class=\"katex\">...</span>", "..."]
 *
 * Nothing from this script executes in visitors' browsers.
 * Invalid input or unsupported mathematics fails the build with a
 * diagnostic on stderr. stdout is reserved for the JSON result.
 */

const fs = require("node:fs");
const katex = require("katex");

function main() {
  const formulas = JSON.parse(fs.readFileSync(0, "utf8"));

  if (!Array.isArray(formulas)) {
    throw new Error("Expected a JSON array of formulas.");
  }

  const rendered = formulas.map((formula, index) => {
    if (
      formula === null ||
      typeof formula !== "object" ||
      typeof formula.tex !== "string" ||
      typeof formula.display !== "boolean"
    ) {
      throw new Error(
        `Formula ${index + 1}: expected {tex: string, display: boolean}.`
      );
    }

    try {
      return katex.renderToString(formula.tex, {
        displayMode: formula.display,
        output: "htmlAndMathml",
        throwOnError: true,
        trust: false,
        strict: "error",
        maxExpand: 1000,
        maxSize: 20
      });
    } catch (error) {
      throw new Error(`Formula ${index + 1}: ${error.message}`);
    }
  });

  process.stdout.write(JSON.stringify(rendered));
}

try {
  main();
} catch (error) {
  process.stderr.write(`Math rendering failed: ${error.message}\n`);
  process.exitCode = 1;
}
