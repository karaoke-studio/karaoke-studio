#pragma once

#include <cstdint>

namespace krok::subtitle::native {

struct BackendDiagnostics {
    std::uint64_t cacheHits = 0;
    std::uint64_t cacheMisses = 0;
    std::uint64_t estimatedCacheBytes = 0;
    std::uint64_t lineCount = 0;
    std::uint64_t charCount = 0;
    std::uint64_t geometryCount = 0;
    std::uint64_t glyphGeometryCacheHits = 0;
    std::uint64_t glyphGeometryCacheMisses = 0;
    std::uint64_t glyphGeometryCacheSize = 0;
    std::uint64_t glyphGeometryCacheEvictions = 0;
    std::uint64_t glyphGeometryCacheCapacity = 0;
    std::uint64_t glyphStrokeCacheHits = 0;
    std::uint64_t glyphStrokeCacheMisses = 0;
    double glyphGeometryBuildMs = 0.0;
    double glyphStrokeBuildMs = 0.0;
    std::uint64_t vectorGlyphCacheHits = 0;
    std::uint64_t vectorGlyphCacheMisses = 0;
    std::uint64_t vectorGlyphCacheSize = 0;
    std::uint64_t vectorGlyphCacheEvictions = 0;
    std::uint64_t vectorGlyphCacheCapacity = 0;
    double vectorGlyphBuildMs = 0.0;
    std::uint64_t imageCacheHits = 0;
    std::uint64_t imageCacheMisses = 0;
    std::uint64_t imageCacheSize = 0;
    std::uint64_t imageCacheEvictions = 0;
    std::uint64_t imageCacheCapacity = 0;
    double imageBuildMs = 0.0;
    std::uint64_t rubyCount = 0;
    std::uint64_t styleCount = 0;
    bool videoMemoryInfoAvailable = false;
    std::uint64_t localVideoMemoryUsageBytes = 0;
    std::uint64_t localVideoMemoryBudgetBytes = 0;
    std::uint64_t nonLocalVideoMemoryUsageBytes = 0;
    std::uint64_t nonLocalVideoMemoryBudgetBytes = 0;
    bool countersEnabled = true;
    std::uint64_t framesRendered = 0;
    std::uint64_t brushCreated = 0;
    std::uint64_t geometryCreatedStable = 0;
    std::uint64_t geometryCreatedDynamic = 0;
    std::uint64_t realizationHit = 0;
    std::uint64_t realizationMiss = 0;
    std::uint64_t strokeDraw = 0;
    std::uint64_t stroke2Draw = 0;
    std::uint64_t glowSourceAreaPx = 0;
    std::uint64_t layerPush = 0;
    double animationLayoutMs = 0.0;
    double geometryMs = 0.0;
    double strokeMs = 0.0;
    double glowMs = 0.0;
    double gpuWaitMs = 0.0;
    double readbackCopyMs = 0.0;
    bool resourceCacheEnabled = true;
    std::uint64_t brushCacheHits = 0;
    std::uint64_t brushCacheMisses = 0;
    std::uint64_t brushCacheEvictions = 0;
    std::uint64_t brushCacheInvalidations = 0;
    std::uint64_t brushCacheSize = 0;
    std::uint64_t brushCacheCapacity = 0;
    bool realizationEnabled = true;
    bool realizationSupported = false;
    bool realizationPrewarmComplete = true;
    std::uint64_t realizationCount = 0;
    std::uint64_t realizationCapacity = 0;
    std::uint64_t realizationPrewarmTasks = 0;
    std::uint64_t realizationPrewarmSkipped = 0;
    double realizationPrewarmMs = 0.0;
    std::uint64_t realizationPrewarmFillTasks = 0;
    std::uint64_t realizationPrewarmStrokeTasks = 0;
    double realizationPrewarmContextMs = 0.0;
    double realizationPrewarmWaitMs = 0.0;
    double realizationPrewarmFillCreateMs = 0.0;
    double realizationPrewarmStrokeCreateMs = 0.0;
    double realizationPrewarmPublishMs = 0.0;
    double realizationPrewarmCreateP50Ms = 0.0;
    double realizationPrewarmCreateP95Ms = 0.0;
    double realizationPrewarmCreateMaxMs = 0.0;
    bool glowDirtyRectEnabled = true;
    // ---- 资源分类计数（2026-10 低显存观测）----
    // D2D/DWrite 不暴露真实分配字节，以下为保守估算，供总量观测与低配机
    // 诊断；全部计入 estimatedCacheBytes（池侧跨 worker 求和后即为总占用）。
    // 各类「计数上限」彼此独立（字体 face 缓存无上限、帧仓按槽数、发光
    // 缓存按张数），字节分类是唯一能回答「总共占了多少」的口径。
    std::uint64_t fontFaceCount = 0;
    std::uint64_t fontFaceBytesEstimate = 0;
    std::uint64_t frameStoreSlots = 0;
    std::uint64_t frameStoreBytesEstimate = 0;
    std::uint64_t glowCacheBytesEstimate = 0;
    std::uint64_t realizationResourceBytesEstimate = 0;
};

}  // namespace krok::subtitle::native
