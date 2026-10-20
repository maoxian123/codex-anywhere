import test from "node:test";
import assert from "node:assert/strict";
import { imageSource, safeLink } from "./utils.ts";
import type { Asset } from "./types.ts";

test("links reject executable protocols and arbitrary local paths", () => {
  for (const value of [
    "javascript:alert(1)",
    "data:text/html,<script>bad</script>",
    "file:///etc/passwd",
    "F:\\project\\secret.txt",
    "//evil.example",
    "/api/assets/private",
    "/other",
  ])
    assert.equal(safeLink(value), undefined);
  assert.equal(
    safeLink("https://example.com/path?q=1"),
    "https://example.com/path?q=1",
  );
  assert.equal(
    safeLink("/tasks/thread-123?item=item_1"),
    "/tasks/thread-123?item=item_1",
  );
});

test("images use authenticated asset URLs or HTTPS, never direct filesystem or executable input", () => {
  for (const value of [
    "javascript:alert(1)",
    "data:image/svg+xml,bad",
    "file:///etc/passwd",
    "F:\\project\\result.png",
    "http://example.com/image.png",
    "//example.com/image.png",
    "/api/assets/a?token=secret",
  ])
    assert.equal(imageSource(value), undefined);
  assert.equal(imageSource("/api/assets/asset-123"), "/api/assets/asset-123");
  assert.equal(
    imageSource("https://example.com/image.png"),
    "https://example.com/image.png",
  );
});

test("local images resolve only to attachments supplied with that item", () => {
  const assets: Asset[] = [
    {
      id: "a",
      name: "one.png",
      source_path: "F:\\work\\one.png",
      mime: "image/png",
      size: 10,
      url: "/api/assets/a",
    },
    {
      id: "b",
      name: "two.png",
      source_path: "/work/two.png",
      mime: "image/png",
      size: 20,
      url: "/api/assets/b",
    },
  ];
  assert.equal(imageSource("F:\\work\\one.png", assets), "/api/assets/a");
  assert.equal(imageSource("/work/two.png", assets), "/api/assets/b");
  assert.equal(imageSource("two.png", assets), "/api/assets/b");
  assert.equal(imageSource("/etc/secret.png", assets), undefined);
  assert.equal(
    imageSource("one.png", [{ ...assets[0], url: "javascript:bad()" }]),
    undefined,
  );
});

test("exact image paths win over ambiguous basenames and preserve POSIX case", () => {
  const assets: Asset[] = [
    {
      id: "a",
      name: "plot.png",
      source_path: "/work/A/plot.png",
      mime: "image/png",
      size: 10,
      url: "/api/assets/a",
    },
    {
      id: "b",
      name: "plot.png",
      source_path: "/work/a/plot.png",
      mime: "image/png",
      size: 10,
      url: "/api/assets/b",
    },
  ];
  assert.equal(imageSource("/work/a/plot.png", assets), "/api/assets/b");
  assert.equal(imageSource("/work/A/plot.png", assets), "/api/assets/a");
  assert.equal(imageSource("plot.png", assets), undefined);
  assert.equal(imageSource("/unrelated/file.png", [assets[0]]), undefined);
});
