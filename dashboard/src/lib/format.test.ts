import { describe, expect, it } from "vitest";
import { ago, daysLeft, pct, price, shortSymbol, signedUsd, tone, usd } from "./format";

describe("format", () => {
  it("formats money with sign and grouping", () => {
    expect(usd(1234.5)).toBe("$1,234.50");
    expect(usd(-0.5)).toBe("-$0.50");
    expect(signedUsd(12)).toBe("+$12.00");
    expect(signedUsd(-3)).toBe("-$3.00");
    expect(usd(null)).toBe("n/a");
    expect(usd(Number.NaN)).toBe("n/a");
  });
  it("formats fractions as percentages", () => {
    expect(pct(0.0412)).toBe("4.12%");
    expect(pct(undefined)).toBe("n/a");
  });
  it("keeps useful precision for prices of any size", () => {
    expect(price(64123.456)).toBe("64,123.46");
    expect(price(2.5)).toBe("2.5");
    expect(price(0.0123456)).toBe("0.01235");
    expect(price(0.0000123)).toBe("0.0000123");
  });
  it("shortens symbols and picks tones", () => {
    expect(shortSymbol("1000PEPE/USDT:USDT")).toBe("1000PEPE");
    expect(tone(1)).toBe("up");
    expect(tone(-1)).toBe("down");
    expect(tone(0)).toBe("mute");
    expect(tone(null)).toBe("mute");
  });
  it("describes elapsed time and subscription days", () => {
    const now = Date.parse("2026-03-05T12:00:00Z");
    expect(ago("2026-03-05T11:59:30Z", now)).toBe("30s ago");
    expect(ago("2026-03-05T11:00:00Z", now)).toBe("1h ago");
    expect(ago("2026-03-02T12:00:00Z", now)).toBe("3d ago");
    expect(ago(null, now)).toBe("never");
    expect(daysLeft("2026-03-15T12:00:00Z", now)).toBe(10);
    expect(daysLeft(null, now)).toBeNull();
  });
});
