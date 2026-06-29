import { describe, it, expect } from "vitest";
import { render, screen } from "@testing-library/react";
import { ErrorState } from "./ErrorState";

describe("ErrorState", () => {
  it("renders the message", () => {
    render(<ErrorState message="Something went wrong" />);
    expect(screen.getByText("Something went wrong")).toBeInTheDocument();
  });

  it("renders empty message", () => {
    render(<ErrorState message="" />);
    const el = document.querySelector(".glass");
    expect(el).toBeInTheDocument();
    expect(el?.textContent).toBe("");
  });
});
