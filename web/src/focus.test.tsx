import "./test-dom";
import test, { afterEach, beforeEach } from "node:test";
import assert from "node:assert/strict";
import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import Composer from "./Composer";
import { Modal } from "./components";
import type { Thread } from "./types";

const originalWidth = window.innerWidth;
const originalShowModal = HTMLDialogElement.prototype.showModal;
const originalClose = HTMLDialogElement.prototype.close;
beforeEach(() => {
  // Model the browser's native showModal focus step, which selects the first
  // focusable control (the close button) after React has applied autoFocus.
  HTMLDialogElement.prototype.showModal = function () {
    this.setAttribute("open", "");
    this.querySelector<HTMLButtonElement>("button")?.focus();
  };
});
afterEach(() => {
  cleanup();
  sessionStorage.clear();
  Object.defineProperty(window, "innerWidth", {
    configurable: true,
    value: originalWidth,
  });
  HTMLDialogElement.prototype.showModal = originalShowModal;
  HTMLDialogElement.prototype.close = originalClose;
});

const thread: Thread = {
  id: "focus-thread",
  host_id: "focus-host",
  title: "Focus",
  cwd: "F:\\focus",
  status: "idle",
  created_at: "now",
  updated_at: "now",
};
const props = {
  thread,
  hostOnline: true,
  onSend: async () => {},
  onInterrupt: async () => {},
  onNavigate: () => {},
};

test("a new-conversation composer focuses only when explicitly requested on desktop", () => {
  for (const [width, autoFocus, expected] of [
    [1024, true, true],
    [680, true, false],
    [1024, false, false],
  ] as const) {
    Object.defineProperty(window, "innerWidth", {
      configurable: true,
      value: width,
    });
    const view = render(<Composer {...props} autoFocus={autoFocus} />);
    const input = screen.getByRole("textbox", { name: "发送消息给 Codex" });
    assert.equal(document.activeElement === input, expected);
    view.unmount();
  }
  const existing = render(<Composer {...props} />);
  assert.notEqual(
    document.activeElement,
    screen.getByRole("textbox", { name: "发送消息给 Codex" }),
  );
  existing.unmount();
});

test("modal focus skips its close button and disabled, readonly or hidden fields", () => {
  render(
    <Modal title="Edit" onClose={() => {}}>
      <input aria-label="Disabled" disabled />
      <input aria-label="Readonly" readOnly />
      <input type="hidden" />
      <div hidden>
        <input aria-label="Hidden" />
      </div>
      <textarea aria-label="Editable" />
      <select aria-label="Later">
        <option>Default</option>
      </select>
    </Modal>,
  );
  assert.equal(
    document.activeElement,
    screen.getByRole("textbox", { name: "Editable" }),
  );
});

test("an autofocused rename field receives focus after the native modal focus step", () => {
  render(
    <Modal title="Edit" onClose={() => {}}>
      <input aria-label="Requested" autoFocus />
    </Modal>,
  );
  assert.equal(
    document.activeElement,
    screen.getByRole("textbox", { name: "Requested" }),
  );
});

test("modal can focus an enabled select and falls back to its first button", () => {
  const editor = render(
    <Modal title="Choose" onClose={() => {}}>
      <select aria-label="Choice">
        <option>Default</option>
      </select>
    </Modal>,
  );
  assert.equal(
    document.activeElement,
    screen.getByRole("combobox", { name: "Choice" }),
  );
  editor.unmount();
  render(
    <Modal title="Information" onClose={() => {}}>
      <p>Read this</p>
    </Modal>,
  );
  assert.equal(
    document.activeElement,
    screen.getByRole("button", { name: "关闭窗口" }),
  );
});

test("modal retains native close and cancel behavior including native focus restoration", () => {
  let previousFocus: Element | null = null;
  let closes = 0;
  let cancels = 0;
  HTMLDialogElement.prototype.showModal = function () {
    previousFocus = document.activeElement;
    this.setAttribute("open", "");
    this.querySelector<HTMLButtonElement>("button")?.focus();
  };
  HTMLDialogElement.prototype.close = function () {
    closes += 1;
    this.removeAttribute("open");
    if (previousFocus instanceof HTMLElement) previousFocus.focus();
  };
  const launcher = document.createElement("button");
  launcher.textContent = "Launch";
  document.body.append(launcher);
  launcher.focus();
  const view = render(
    <Modal
      title="Edit"
      onClose={() => {
        cancels += 1;
      }}
    >
      <input aria-label="Value" />
    </Modal>,
  );
  fireEvent(screen.getByRole("dialog"), new Event("cancel"));
  assert.equal(cancels, 1);
  view.unmount();
  assert.equal(closes, 1);
  assert.equal(document.activeElement, launcher);
  launcher.remove();
});
