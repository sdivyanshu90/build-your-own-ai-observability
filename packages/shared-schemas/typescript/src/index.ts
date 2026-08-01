/**
 * Shared schemas for the AI Observability Platform (TypeScript).
 *
 * The contract boundary between the TypeScript SDK, the web application and the
 * ingestion API. Contains no I/O and no framework dependency, so it can be
 * imported by a browser bundle as safely as by a Node service.
 */

export * from "./canonical.js";
export * as semconv from "./semconv.js";
export * from "./types.js";
export * from "./ids.js";

export const SCHEMA_VERSION = "1.0";
export const VERSION = "0.1.0";
