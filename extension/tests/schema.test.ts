/**
 * Gemini rejects the WHOLE request when one declaration is malformed, not just
 * the bad tool — so a mistake here takes all twelve down at once, and the
 * symptom is an opaque 400 rather than anything pointing at the cause.
 */

import { describe, expect, it } from "vitest";

import { toFunctionDeclaration, toFunctionDeclarations } from "../src/llm/schema.js";
import { TOOLS, TOOLS_BY_NAME, type ToolDef } from "../src/tools/registry.js";

const GEMINI_TYPES = new Set(["STRING", "NUMBER", "INTEGER", "BOOLEAN", "ARRAY", "OBJECT"]);

describe("every real tool converts to something Gemini accepts", () => {
  const decls = toFunctionDeclarations(TOOLS);

  it("produces one declaration per tool", () => {
    expect(decls).toHaveLength(TOOLS.length);
  });

  it("uses only uppercase Gemini type names", () => {
    for (const d of decls) {
      if (!d.parameters) continue;
      expect(d.parameters.type, d.name).toBe("OBJECT");
      for (const [prop, schema] of Object.entries(d.parameters.properties ?? {})) {
        expect(GEMINI_TYPES.has(schema.type), `${d.name}.${prop} = ${schema.type}`).toBe(true);
      }
    }
  });

  it("never emits a JSON Schema keyword Gemini rejects", () => {
    const serialized = JSON.stringify(decls);
    for (const banned of ["$ref", "$defs", "anyOf", "oneOf", "allOf", "additionalProperties"]) {
      expect(serialized, `${banned} leaked into the declarations`).not.toContain(banned);
    }
  });

  it("marks every required parameter as one the tool actually declares", () => {
    for (const d of decls) {
      for (const req of d.parameters?.required ?? []) {
        expect(Object.keys(d.parameters?.properties ?? {}), `${d.name}.${req}`).toContain(req);
      }
    }
  });

  it("keeps descriptions, which are what the model selects on", () => {
    for (const d of decls) {
      expect(d.description.length, d.name).toBeGreaterThan(80);
    }
  });
});

describe("the shapes that would silently break a call", () => {
  const make = (properties: Record<string, unknown>, required?: string[]): ToolDef =>
    ({
      name: "t",
      description: "x".repeat(90),
      parameters: { type: "object", properties, ...(required ? { required } : {}) },
      handler: async () => ({}),
    }) as unknown as ToolDef;

  it("omits `parameters` entirely for a no-argument tool", () => {
    // An OBJECT with an empty properties map is rejected as malformed, so
    // get_status must send no parameters block at all.
    const decl = toFunctionDeclaration(make({}));
    expect(decl.parameters).toBeUndefined();
    expect(toFunctionDeclaration(TOOLS_BY_NAME["get_status"]!).parameters).toBeUndefined();
  });

  it("collapses a nullable union to one type plus nullable", () => {
    // `target_percentage?: number` is the real case: an optional parameter
    // compiles to ["number", "null"], which Gemini will not take.
    const decl = toFunctionDeclaration(
      make({ target: { type: ["number", "null"], description: "d" } }),
    );
    expect(decl.parameters!.properties!["target"]).toEqual({
      type: "NUMBER",
      description: "d",
      nullable: true,
    });
  });

  it("falls back to STRING on an unknown type rather than failing the request", () => {
    // One odd type must not take the other eleven tools down with it.
    const decl = toFunctionDeclaration(make({ weird: { type: "date-time", description: "d" } }));
    expect(decl.parameters!.properties!["weird"]!.type).toBe("STRING");
  });

  it("converts nested arrays and objects", () => {
    const decl = toFunctionDeclaration(
      make({
        ids: { type: "array", items: { type: "integer" }, description: "d" },
        nested: {
          type: "object",
          description: "d",
          properties: { inner: { type: "boolean", description: "d" } },
          required: ["inner"],
        },
      }),
    );
    expect(decl.parameters!.properties!["ids"]!.items!.type).toBe("INTEGER");
    expect(decl.parameters!.properties!["nested"]!.properties!["inner"]!.type).toBe("BOOLEAN");
    expect(decl.parameters!.properties!["nested"]!.required).toEqual(["inner"]);
  });

  it("drops an empty required list rather than sending required: []", () => {
    const decl = toFunctionDeclaration(make({ a: { type: "string", description: "d" } }, []));
    expect(decl.parameters!.required).toBeUndefined();
  });
});
