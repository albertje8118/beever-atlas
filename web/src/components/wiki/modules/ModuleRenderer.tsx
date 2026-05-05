/**
 * Adaptive page module dispatcher.
 *
 * Reads `page.modules[]` and routes each entry to its dedicated React
 * component. The switch on `module.id` is the single source of truth
 * for what module IDs are recognised — adding a new module type
 * requires (a) adding the catalog entry on the backend and (b)
 * adding both a component file and a switch case here.
 *
 * Compiler-rendered modules (markdown-emitting: key_facts,
 * decision_log, timeline, comparison_matrix, pros_cons,
 * quote_highlights, flow_chart, entity_diagram, open_questions,
 * subpage_cards, related_threads) all use the shared `MarkdownModule`
 * wrapper which renders the pre-rendered markdown via WikiMarkdown
 * with citation awareness.
 *
 * Frontend-only modules (media_*, link_card, pdf_preview,
 * video_embed) get specialized components in Phase 7 — for v1 they
 * fall through to MarkdownModule (which renders the markdown the
 * compiler produced for them, if any).
 */

import type { WikiCitation, WikiPageModule } from "@/lib/types";
import { HeroSummaryModule } from "./HeroSummaryModule";
import { KeyFactsModule } from "./KeyFactsModule";
import { DecisionLogModule } from "./DecisionLogModule";
import { TimelineModule } from "./TimelineModule";
import { ComparisonMatrixModule } from "./ComparisonMatrixModule";
import { ProsConsModule } from "./ProsConsModule";
import { QuoteHighlightsModule } from "./QuoteHighlightsModule";
import { FlowChartModule } from "./FlowChartModule";
import { EntityDiagramModule } from "./EntityDiagramModule";
import { OpenQuestionsModule } from "./OpenQuestionsModule";
import { SubpageCardsModule } from "./SubpageCardsModule";
import { RelatedThreadsModule } from "./RelatedThreadsModule";
import { MediaHeroModule } from "./MediaHeroModule";
import { MediaInlineModule } from "./MediaInlineModule";
import { MediaGalleryModule } from "./MediaGalleryModule";
import { LinkCardModule } from "./LinkCardModule";
import { PdfPreviewModule } from "./PdfPreviewModule";
import { VideoEmbedModule } from "./VideoEmbedModule";
import { ProvenanceDrawerModule } from "./ProvenanceDrawerModule";
import { AcronymLegendModule } from "./AcronymLegendModule";
import { StatStripModule } from "./StatStripModule";

export interface ModuleProps {
  module: WikiPageModule;
  citations: WikiCitation[];
  onNavigate?: (pageId: string) => void;
}

interface ModuleRendererProps {
  modules: WikiPageModule[];
  citations: WikiCitation[];
  onNavigate?: (pageId: string) => void;
}

export function ModuleRenderer({
  modules,
  citations,
  onNavigate,
}: ModuleRendererProps) {
  return (
    <>
      {modules.map((module) => {
        const props: ModuleProps = { module, citations, onNavigate };
        switch (module.id) {
          case "hero_summary":
            return <HeroSummaryModule key={module.anchor} {...props} />;
          case "key_facts":
            return <KeyFactsModule key={module.anchor} {...props} />;
          case "decision_log":
            return <DecisionLogModule key={module.anchor} {...props} />;
          case "timeline":
            return <TimelineModule key={module.anchor} {...props} />;
          case "comparison_matrix":
            return <ComparisonMatrixModule key={module.anchor} {...props} />;
          case "pros_cons":
            return <ProsConsModule key={module.anchor} {...props} />;
          case "quote_highlights":
            return <QuoteHighlightsModule key={module.anchor} {...props} />;
          case "flow_chart":
            return <FlowChartModule key={module.anchor} {...props} />;
          case "entity_diagram":
            return <EntityDiagramModule key={module.anchor} {...props} />;
          case "open_questions":
            return <OpenQuestionsModule key={module.anchor} {...props} />;
          case "subpage_cards":
            return <SubpageCardsModule key={module.anchor} {...props} />;
          case "related_threads":
            return <RelatedThreadsModule key={module.anchor} {...props} />;
          // Media modules — frontend-only renderers consume the
          // structured ``module.data`` payload (URLs, captions,
          // items list) the orchestrator's ``_extract_media_for_module``
          // populated. These bypass the markdown path entirely.
          case "media_hero":
            return <MediaHeroModule key={module.anchor} {...props} />;
          case "media_inline":
            return <MediaInlineModule key={module.anchor} {...props} />;
          case "media_gallery":
            return <MediaGalleryModule key={module.anchor} {...props} />;
          case "link_card":
            return <LinkCardModule key={module.anchor} {...props} />;
          case "pdf_preview":
            return <PdfPreviewModule key={module.anchor} {...props} />;
          case "video_embed":
            return <VideoEmbedModule key={module.anchor} {...props} />;
          // Provenance + reading-aid modules — frontend-only renderers
          // consume the structured ``module.data`` payload built by the
          // matching Python builder (provenance_drawer / acronym_legend
          // / stat_strip). All 3 are content-fullness modules added to
          // give human readers + LLM agents richer access to the same
          // source data.
          case "stat_strip":
            return <StatStripModule key={module.anchor} {...props} />;
          case "acronym_legend":
            return <AcronymLegendModule key={module.anchor} {...props} />;
          case "provenance_drawer":
            return <ProvenanceDrawerModule key={module.anchor} {...props} />;
          default:
            // Unknown module ID — silently drop. The backend's
            // validator should have rejected it before persistence;
            // surfacing a fallback would mask a backend bug.
            return null;
        }
      })}
    </>
  );
}
