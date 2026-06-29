import tseslint from "@typescript-eslint/eslint-plugin";
import tsparser from "@typescript-eslint/parser";
import react from "eslint-plugin-react";

export default [
  {
    files: ["src/**/*.{ts,tsx}"],
    languageOptions: {
      parser: tsparser,
      parserOptions: { ecmaVersion: "latest", ecmaFeatures: { jsx: true }, sourceType: "module" },
      globals: { window: "readonly", document: "readonly", fetch: "readonly", console: "readonly", setTimeout: "readonly", clearTimeout: "readonly", AbortController: "readonly", FormData: "readonly", URL: "readonly", URLSearchParams: "readonly", Response: "readonly", Request: "readonly", Headers: "readonly", DOMException: "readonly", import: "readonly", module: "readonly", require: "readonly", process: "readonly", __dirname: "readonly", __filename: "readonly", Buffer: "readonly", global: "readonly" },
    },
    plugins: { "@typescript-eslint": tseslint, react },
    rules: {
      "react/jsx-uses-react": "error",
      "react/jsx-uses-vars": "error",
      "@typescript-eslint/no-unused-vars": ["warn", { argsIgnorePattern: "^_" }],
    },
    settings: { react: { version: "detect" } },
  },
];
