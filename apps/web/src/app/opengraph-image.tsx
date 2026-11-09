import { ImageResponse } from "next/og";

export const alt = "Memory Lab: benchmark how game-playing agents use memory";
export const size = { width: 1200, height: 630 };
export const contentType = "image/png";

export default function Image() {
  return new ImageResponse(
    (
      <div style={{ width: "100%", height: "100%", display: "flex", flexDirection: "column", justifyContent: "center", padding: 80, background: "#14213d", color: "#f4f1de" }}>
        <div style={{ fontSize: 96, fontWeight: 700 }}>Memory Lab</div>
        <div style={{ fontSize: 40, marginTop: 24 }}>Does an agent&apos;s own written memory help it play?</div>
        <div style={{ fontSize: 28, marginTop: 40, opacity: 0.8 }}>Every run logged, replayable and forkable.</div>
      </div>
    ),
    size,
  );
}
