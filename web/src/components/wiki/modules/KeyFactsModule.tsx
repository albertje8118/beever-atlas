/** Key Facts module — renders the compiler-emitted GFM table.
 *  Phase 7+ may swap this for a sortable table component. For v1 the
 *  shared markdown wrapper is sufficient. */
import { MarkdownModule } from "./MarkdownModule";
import type { ModuleProps } from "./ModuleRenderer";

export function KeyFactsModule(props: ModuleProps) {
  return <MarkdownModule {...props} />;
}
