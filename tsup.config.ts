import { type Options, defineConfig } from "tsup";

const entry = {
  index: "src/index.ts",
  webtorrent: "src/engines/webtorrent.ts",
};

const baseOptions: Options = {
  clean: true,
  minify: false,
  skipNodeModulesBundle: true,
  sourcemap: true,
  target: "es2022",
  tsconfig: "./tsconfig.json",
  keepNames: true,
  cjsInterop: true,
  splitting: true,
  external: ["pmtiles", "webtorrent"],
};

export default [
  defineConfig({
    ...baseOptions,
    entry,
    outDir: "dist/cjs",
    format: "cjs",
    dts: true,
  }),
  defineConfig({
    ...baseOptions,
    entry,
    outDir: "dist/esm",
    format: "esm",
    dts: true,
  }),
];
