/**
 * JSON Schema -> Gemini FunctionDeclaration parameters.
 *
 * Gemini does not take arbitrary JSON Schema. It accepts a narrowed
 * OpenAPI-flavoured subset, and rejects the whole request — not just the bad
 * field — when it sees something outside it. The differences that bite:
 *
 *   1. `type` is an ENUM IN UPPERCASE: STRING, NUMBER, INTEGER, BOOLEAN,
 *      ARRAY, OBJECT. Lowercase "integer" is rejected.
 *   2. No `$ref` / `$defs`. Anything referenced has to be inlined.
 *   3. No union types. `["string", "null"]` or an `anyOf` becomes a single
 *      type plus `nullable: true` — which is exactly what an optional
 *      parameter like `target_percentage?: number` compiles to.
 *   4. Unknown keywords (`additionalProperties`, `default`, `examples`,
 *      `format` on a non-string) are dropped rather than passed through.
 *
 * Keeping this conversion in one place means the registry can stay plain JSON
 * Schema, which is what a future OpenAI or Anthropic adapter would also want.
 */

import type { JsonSchema, ToolDef } from "../tools/registry.js";

export interface GeminiSchema {
  type: string;
  description?: string;
  nullable?: boolean;
  enum?: string[];
  items?: GeminiSchema;
  properties?: Record<string, GeminiSchema>;
  required?: string[];
}

export interface FunctionDeclaration {
  name: string;
  description: string;
  parameters?: GeminiSchema;
}

const TYPE_MAP: Record<string, string> = {
  string: "STRING",
  number: "NUMBER",
  integer: "INTEGER",
  boolean: "BOOLEAN",
  array: "ARRAY",
  object: "OBJECT",
};

/**
 * Normalize one type. A union collapses to its first non-null member, with
 * nullability reported separately.
 */
function normalizeType(raw: unknown): { type: string; nullable: boolean } {
  if (Array.isArray(raw)) {
    const nullable = raw.includes("null");
    const first = raw.find((t) => t !== "null");
    return { type: TYPE_MAP[String(first)] ?? "STRING", nullable };
  }
  const mapped = TYPE_MAP[String(raw)];
  if (!mapped) {
    // Better a permissive STRING than a rejected request: an unknown type here
    // would fail the entire call, taking the other eleven tools with it.
    return { type: "STRING", nullable: false };
  }
  return { type: mapped, nullable: false };
}

function convertProperty(raw: Record<string, unknown>): GeminiSchema {
  const { type, nullable } = normalizeType(raw["type"]);
  const out: GeminiSchema = { type };

  if (typeof raw["description"] === "string") out.description = raw["description"];
  if (nullable || raw["nullable"] === true) out.nullable = true;

  if (Array.isArray(raw["enum"])) {
    out.enum = raw["enum"].map(String);
  }
  if (type === "ARRAY" && raw["items"] && typeof raw["items"] === "object") {
    out.items = convertProperty(raw["items"] as Record<string, unknown>);
  }
  if (type === "OBJECT" && raw["properties"] && typeof raw["properties"] === "object") {
    out.properties = {};
    for (const [k, v] of Object.entries(raw["properties"] as Record<string, unknown>)) {
      if (v && typeof v === "object") {
        out.properties[k] = convertProperty(v as Record<string, unknown>);
      }
    }
    if (Array.isArray(raw["required"])) out.required = raw["required"].map(String);
  }

  return out;
}

export function toFunctionDeclaration(tool: ToolDef): FunctionDeclaration {
  const schema: JsonSchema = tool.parameters;
  const properties: Record<string, GeminiSchema> = {};

  for (const [name, prop] of Object.entries(schema.properties ?? {})) {
    properties[name] = convertProperty(prop as unknown as Record<string, unknown>);
  }

  const decl: FunctionDeclaration = {
    name: tool.name,
    description: tool.description,
  };

  // A tool with no parameters must OMIT `parameters` entirely. Sending an
  // OBJECT with an empty `properties` map is rejected as malformed.
  if (Object.keys(properties).length > 0) {
    decl.parameters = {
      type: "OBJECT",
      properties,
      ...(schema.required?.length ? { required: schema.required } : {}),
    };
  }

  return decl;
}

export function toFunctionDeclarations(tools: ToolDef[]): FunctionDeclaration[] {
  return tools.map(toFunctionDeclaration);
}
