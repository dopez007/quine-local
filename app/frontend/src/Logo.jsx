import React from "react";

// The Quine mark: a Q (ring + tail) with a concentric inner ring — a circle that
// contains itself, the visual of a program that outputs its own source. Stroke uses
// currentColor so it inherits the surrounding text color (works in dark + light).
//
// `animate` plays a one-time "self-writing" draw-on. Unlike the large marketing hero
// (which staggers each stroke), the strokes here draw simultaneously — at 16px a
// staggered reveal reads as flicker, a single smooth draw reads as polish.
export default function Logo({ size = 16, className, title = "Quine", animate = false }) {
  const len = (n) => (animate ? { "--len": n } : undefined);
  const draw = animate ? "q-logo-draw" : undefined;
  return (
    <svg
      width={size}
      height={size}
      viewBox="0 0 24 24"
      fill="none"
      stroke="currentColor"
      strokeWidth="2"
      strokeLinecap="round"
      strokeLinejoin="round"
      className={(className ? className + " " : "") + (animate ? "q-logo-animate" : "")}
      role="img"
      aria-label={title}
    >
      <circle className={draw} style={len(48)} cx="11" cy="11" r="7.5" />
      <circle className={draw} style={len(21)} cx="11" cy="11" r="3.25" />
      <path className={draw} style={len(7)} d="M16.5 16.5L21 21" />
    </svg>
  );
}
