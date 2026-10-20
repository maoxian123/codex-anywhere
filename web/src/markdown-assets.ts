import { unified } from "unified";
import remarkParse from "remark-parse";
import remarkGfm from "remark-gfm";
import type { Asset } from "./types";
import { imageSource } from "./utils";

type MarkdownNode = {
  type: string;
  url?: string;
  identifier?: string;
  children?: MarkdownNode[];
};

const parser = unified().use(remarkParse).use(remarkGfm);

/** Keep attachments in the gallery unless Markdown really renders that asset.
 * Parsing uses the same CommonMark/GFM syntax as the message renderer, so code
 * examples, escaped image syntax, and ambiguous filenames cannot hide files.
 */
export function galleryAssets(text: string, assets: Asset[]): Asset[] {
  if (!assets.length || !text.includes("![")) return assets;
  const tree = parser.parse(text) as MarkdownNode;
  const definitions = new Map<string, string>();
  const embedded = new Set<string>();
  const visit = (
    node: MarkdownNode,
    callback: (node: MarkdownNode) => void,
  ) => {
    callback(node);
    node.children?.forEach((child) => visit(child, callback));
  };
  visit(tree, (node) => {
    if (
      node.type === "definition" &&
      node.identifier &&
      node.url &&
      !definitions.has(node.identifier)
    )
      definitions.set(node.identifier, node.url);
  });
  visit(tree, (node) => {
    const url =
      node.type === "image"
        ? node.url
        : node.type === "imageReference" && node.identifier
          ? definitions.get(node.identifier)
          : undefined;
    const source = imageSource(url, assets);
    if (source) embedded.add(source);
  });
  return assets.filter((asset) => !embedded.has(asset.url));
}
