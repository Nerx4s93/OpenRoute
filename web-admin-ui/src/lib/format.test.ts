import { describe, expect, it } from "vitest";
import {
  formatCompact,
  formatDuration,
  formatLimit,
  formatModels,
  formatNumber,
  requestTokens,
} from "./format";

describe("formatDuration", () => {
  it("rounds sub-minute values and formats minutes/hours in English", () => {
    expect(formatDuration(0, "en")).toBe("<1m");
    expect(formatDuration(59, "en")).toBe("<1m");
    expect(formatDuration(60, "en")).toBe("1m");
    expect(formatDuration(125, "en")).toBe("2m");
    expect(formatDuration(3600, "en")).toBe("1h 00m");
    expect(formatDuration(7320, "en")).toBe("2h 02m");
  });

  it("uses Russian unit suffixes", () => {
    expect(formatDuration(60, "ru")).toBe("1м");
    expect(formatDuration(3600, "ru")).toBe("1ч 00м");
  });
});

describe("formatNumber", () => {
  it("uses English thousands separators", () => {
    expect(formatNumber(12345, "en")).toBe("12,345");
    expect(formatLimit(null, "en")).toBe("—");
    expect(formatLimit(0, "en")).toBe("0");
  });

  it("uses Russian thousands separators", () => {
    expect(formatNumber(12345, "ru")).toMatch(/12\s345/);
  });
});

describe("formatCompact", () => {
  it("uses short English notation", () => {
    expect(formatCompact(0, "en")).toBe("0");
    expect(formatCompact(1500, "en")).toMatch(/1\.5K|K/);
    expect(formatCompact(2_500_000, "en").length).toBeLessThan(
      formatNumber(2_500_000, "en").length,
    );
  });

  it("uses short Russian notation", () => {
    expect(formatCompact(1500, "ru")).toMatch(/1,5|тыс/);
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
