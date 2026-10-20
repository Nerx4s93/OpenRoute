import { describe, expect, it } from "vitest";
import {
  formatDuration,
  formatLimit,
  formatModels,
  formatNumber,
  requestTokens,
} from "./format";

describe("formatDuration", () => {
  it("rounds sub-minute values and formats minutes/hours", () => {
    expect(formatDuration(0)).toBe("<1м");
    expect(formatDuration(59)).toBe("<1м");
    expect(formatDuration(60)).toBe("1м");
    expect(formatDuration(125)).toBe("2м");
    expect(formatDuration(3600)).toBe("1ч 00м");
    expect(formatDuration(7320)).toBe("2ч 02м");
  });
});

describe("formatNumber", () => {
  it("uses Russian thousands separators", () => {
    expect(formatNumber(12345)).toMatch(/12\s345/);
    expect(formatLimit(null)).toBe("—");
    expect(formatLimit(0)).toBe("0");
  });
});

describe("formatModels and requestTokens", () => {
  it("joins models and sums nullable token fields", () => {
    expect(formatModels(["a", "b"])).toBe("a, b");
    expect(formatModels(null)).toBe("—");
    expect(requestTokens(10, 5)).toBe(15);
    expect(requestTokens(null, undefined)).toBe(0);
  });
});
