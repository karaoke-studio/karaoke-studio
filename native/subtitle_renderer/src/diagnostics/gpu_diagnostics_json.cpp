#include "gpu_diagnostics_json.h"

#include <QtCore/QJsonObject>

namespace krok::subtitle::native::diagnostics {

void appendGpuDiagnostics(
    QJsonObject *out,
    const BackendDiagnostics &diagnostics
) {
    out->insert(QStringLiteral("cache_hits"), static_cast<qint64>(diagnostics.cacheHits));
    out->insert(QStringLiteral("cache_misses"), static_cast<qint64>(diagnostics.cacheMisses));
    out->insert(
        QStringLiteral("estimated_cache_bytes"),
        static_cast<qint64>(diagnostics.estimatedCacheBytes)
    );
    out->insert(QStringLiteral("cached_lines"), static_cast<qint64>(diagnostics.lineCount));
    out->insert(QStringLiteral("cached_chars"), static_cast<qint64>(diagnostics.charCount));
    out->insert(
        QStringLiteral("cached_geometries"),
        static_cast<qint64>(diagnostics.geometryCount)
    );
    out->insert(QStringLiteral("cached_rubies"), static_cast<qint64>(diagnostics.rubyCount));
    out->insert(QStringLiteral("cached_styles"), static_cast<qint64>(diagnostics.styleCount));
    out->insert(
        QStringLiteral("video_memory_info_available"),
        diagnostics.videoMemoryInfoAvailable
    );
    out->insert(
        QStringLiteral("local_video_memory_usage_bytes"),
        static_cast<qint64>(diagnostics.localVideoMemoryUsageBytes)
    );
    out->insert(
        QStringLiteral("local_video_memory_budget_bytes"),
        static_cast<qint64>(diagnostics.localVideoMemoryBudgetBytes)
    );
    out->insert(
        QStringLiteral("non_local_video_memory_usage_bytes"),
        static_cast<qint64>(diagnostics.nonLocalVideoMemoryUsageBytes)
    );
    out->insert(
        QStringLiteral("non_local_video_memory_budget_bytes"),
        static_cast<qint64>(diagnostics.nonLocalVideoMemoryBudgetBytes)
    );
    out->insert(QStringLiteral("counters_enabled"), diagnostics.countersEnabled);
    out->insert(
        QStringLiteral("frames_rendered"),
        static_cast<qint64>(diagnostics.framesRendered)
    );
    out->insert(
        QStringLiteral("brush_created"),
        static_cast<qint64>(diagnostics.brushCreated)
    );
    out->insert(
        QStringLiteral("geometry_created_stable"),
        static_cast<qint64>(diagnostics.geometryCreatedStable)
    );
    out->insert(
        QStringLiteral("geometry_created_dynamic"),
        static_cast<qint64>(diagnostics.geometryCreatedDynamic)
    );
    out->insert(
        QStringLiteral("realization_hit"),
        static_cast<qint64>(diagnostics.realizationHit)
    );
    out->insert(
        QStringLiteral("realization_miss"),
        static_cast<qint64>(diagnostics.realizationMiss)
    );
    out->insert(
        QStringLiteral("stroke_draw"),
        static_cast<qint64>(diagnostics.strokeDraw)
    );
    out->insert(
        QStringLiteral("stroke2_draw"),
        static_cast<qint64>(diagnostics.stroke2Draw)
    );
    out->insert(
        QStringLiteral("glow_source_area_px"),
        static_cast<qint64>(diagnostics.glowSourceAreaPx)
    );
    out->insert(
        QStringLiteral("layer_push"),
        static_cast<qint64>(diagnostics.layerPush)
    );
    out->insert(QStringLiteral("animation_layout_ms"), diagnostics.animationLayoutMs);
    out->insert(QStringLiteral("geometry_ms"), diagnostics.geometryMs);
    out->insert(QStringLiteral("stroke_ms"), diagnostics.strokeMs);
    out->insert(QStringLiteral("glow_ms"), diagnostics.glowMs);
    out->insert(QStringLiteral("gpu_wait_ms"), diagnostics.gpuWaitMs);
    out->insert(QStringLiteral("readback_copy_ms"), diagnostics.readbackCopyMs);
    out->insert(
        QStringLiteral("resource_cache_enabled"),
        diagnostics.resourceCacheEnabled
    );
    out->insert(
        QStringLiteral("brush_cache_hits"),
        static_cast<qint64>(diagnostics.brushCacheHits)
    );
    out->insert(
        QStringLiteral("brush_cache_misses"),
        static_cast<qint64>(diagnostics.brushCacheMisses)
    );
    out->insert(
        QStringLiteral("brush_cache_evictions"),
        static_cast<qint64>(diagnostics.brushCacheEvictions)
    );
    out->insert(
        QStringLiteral("brush_cache_invalidations"),
        static_cast<qint64>(diagnostics.brushCacheInvalidations)
    );
    out->insert(
        QStringLiteral("brush_cache_size"),
        static_cast<qint64>(diagnostics.brushCacheSize)
    );
    out->insert(
        QStringLiteral("brush_cache_capacity"),
        static_cast<qint64>(diagnostics.brushCacheCapacity)
    );
    out->insert(
        QStringLiteral("realization_enabled"),
        diagnostics.realizationEnabled
    );
    out->insert(
        QStringLiteral("realization_supported"),
        diagnostics.realizationSupported
    );
    out->insert(
        QStringLiteral("realization_prewarm_complete"),
        diagnostics.realizationPrewarmComplete
    );
    out->insert(
        QStringLiteral("realization_count"),
        static_cast<qint64>(diagnostics.realizationCount)
    );
    out->insert(
        QStringLiteral("realization_capacity"),
        static_cast<qint64>(diagnostics.realizationCapacity)
    );
    out->insert(
        QStringLiteral("realization_prewarm_tasks"),
        static_cast<qint64>(diagnostics.realizationPrewarmTasks)
    );
    out->insert(
        QStringLiteral("realization_prewarm_skipped"),
        static_cast<qint64>(diagnostics.realizationPrewarmSkipped)
    );
    out->insert(
        QStringLiteral("realization_prewarm_ms"),
        diagnostics.realizationPrewarmMs
    );
    out->insert(
        QStringLiteral("realization_prewarm_fill_tasks"),
        static_cast<qint64>(diagnostics.realizationPrewarmFillTasks)
    );
    out->insert(
        QStringLiteral("realization_prewarm_stroke_tasks"),
        static_cast<qint64>(diagnostics.realizationPrewarmStrokeTasks)
    );
    out->insert(
        QStringLiteral("realization_prewarm_context_ms"),
        diagnostics.realizationPrewarmContextMs
    );
    out->insert(
        QStringLiteral("realization_prewarm_wait_ms"),
        diagnostics.realizationPrewarmWaitMs
    );
    out->insert(
        QStringLiteral("realization_prewarm_fill_create_ms"),
        diagnostics.realizationPrewarmFillCreateMs
    );
    out->insert(
        QStringLiteral("realization_prewarm_stroke_create_ms"),
        diagnostics.realizationPrewarmStrokeCreateMs
    );
    out->insert(
        QStringLiteral("realization_prewarm_publish_ms"),
        diagnostics.realizationPrewarmPublishMs
    );
    out->insert(
        QStringLiteral("realization_prewarm_create_p50_ms"),
        diagnostics.realizationPrewarmCreateP50Ms
    );
    out->insert(
        QStringLiteral("realization_prewarm_create_p95_ms"),
        diagnostics.realizationPrewarmCreateP95Ms
    );
    out->insert(
        QStringLiteral("realization_prewarm_create_max_ms"),
        diagnostics.realizationPrewarmCreateMaxMs
    );
    out->insert(
        QStringLiteral("glow_dirty_rect_enabled"),
        diagnostics.glowDirtyRectEnabled
    );
}

void appendGpuFrameDiagnostics(
    QJsonObject *out,
    const ProbeResult::FrameDiagnostics &diagnostics
) {
    out->insert(QStringLiteral("counters_enabled"), diagnostics.countersEnabled);
    out->insert(QStringLiteral("brush_created"), static_cast<qint64>(diagnostics.brushCreated));
    out->insert(QStringLiteral("geometry_created_stable"), static_cast<qint64>(diagnostics.geometryCreatedStable));
    out->insert(QStringLiteral("geometry_created_dynamic"), static_cast<qint64>(diagnostics.geometryCreatedDynamic));
    out->insert(QStringLiteral("realization_hit"), static_cast<qint64>(diagnostics.realizationHit));
    out->insert(QStringLiteral("realization_miss"), static_cast<qint64>(diagnostics.realizationMiss));
    out->insert(QStringLiteral("stroke_draw"), static_cast<qint64>(diagnostics.strokeDraw));
    out->insert(QStringLiteral("stroke2_draw"), static_cast<qint64>(diagnostics.stroke2Draw));
    out->insert(QStringLiteral("glow_source_area_px"), static_cast<qint64>(diagnostics.glowSourceAreaPx));
    out->insert(QStringLiteral("layer_push"), static_cast<qint64>(diagnostics.layerPush));
    out->insert(QStringLiteral("animation_layout_ms"), diagnostics.animationLayoutMs);
    out->insert(QStringLiteral("geometry_ms"), diagnostics.geometryMs);
    out->insert(QStringLiteral("stroke_ms"), diagnostics.strokeMs);
    out->insert(QStringLiteral("glow_ms"), diagnostics.glowMs);
    out->insert(QStringLiteral("end_draw_wait_ms"), diagnostics.endDrawWaitMs);
    out->insert(QStringLiteral("end_draw_glow_source_ms"), diagnostics.endDrawGlowSourceMs);
    out->insert(QStringLiteral("end_draw_ruby_glow_source_ms"), diagnostics.endDrawRubyGlowSourceMs);
    out->insert(QStringLiteral("end_draw_inline_glow_source_ms"), diagnostics.endDrawInlineGlowSourceMs);
    out->insert(QStringLiteral("end_draw_frame_layers_ms"), diagnostics.endDrawFrameLayersMs);
    out->insert(QStringLiteral("end_draw_empty_frame_ms"), diagnostics.endDrawEmptyFrameMs);
    out->insert(QStringLiteral("end_draw_count"), static_cast<qint64>(diagnostics.endDrawCount));
    out->insert(QStringLiteral("end_draw_glow_source_count"), static_cast<qint64>(diagnostics.endDrawGlowSourceCount));
    out->insert(QStringLiteral("end_draw_ruby_glow_source_count"), static_cast<qint64>(diagnostics.endDrawRubyGlowSourceCount));
    out->insert(QStringLiteral("end_draw_inline_glow_source_count"), static_cast<qint64>(diagnostics.endDrawInlineGlowSourceCount));
    out->insert(QStringLiteral("end_draw_frame_layers_count"), static_cast<qint64>(diagnostics.endDrawFrameLayersCount));
    out->insert(QStringLiteral("end_draw_empty_frame_count"), static_cast<qint64>(diagnostics.endDrawEmptyFrameCount));
    out->insert(QStringLiteral("gpu_wait_ms"), diagnostics.gpuWaitMs);
    out->insert(QStringLiteral("readback_copy_ms"), diagnostics.readbackCopyMs);
}

}  // namespace krok::subtitle::native::diagnostics
