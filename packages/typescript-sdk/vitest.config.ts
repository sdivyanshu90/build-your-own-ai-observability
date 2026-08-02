import { defineConfig } from "vitest/config";
import { fileURLToPath } from "node:url";

export default defineConfig({
  resolve: {
    alias: {
      // Test against the shared-schemas *sources*, so a change there is caught
      // here without an intermediate build step.
      "@aiobs/schemas": fileURLToPath(
        new URL("../shared-schemas/typescript/src/index.ts", import.meta.url),
      ),
    },
  },
  test: {
    include: ["test/**/*.test.ts"],
    environment: "node",
  },
});
