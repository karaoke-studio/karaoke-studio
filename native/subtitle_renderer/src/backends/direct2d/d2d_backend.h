#pragma once

#include "../render_backend.h"
#include "d2d_device.h"
#include "native_preview_surface.h"

#include <memory>

namespace krok::subtitle::native {

class Direct2DGpuBackend final : public RenderBackend {
public:
    explicit Direct2DGpuBackend(bool forceWarp);
    Direct2DGpuBackend(
        bool forceWarp,
        std::shared_ptr<D2DDeviceResources> sharedDeviceResources
    );
    ~Direct2DGpuBackend() override;

    BackendCaps capabilities() const override;
    BackendDiagnostics diagnostics() const override;
    ProbeResult renderProbe(const ProbeOptions &options) override;
    void configure(const RenderScene &scene) override;
    ProbeResult renderFrame(int tMs, bool compactBands = false) override;
    NativePreviewResult presentFrame(
        int tMs,
        const NativePreviewTarget &target,
        int generation
    ) override;
    void closeNativePreview() override;
    void pumpNativePreviewMessages() override;
    NativeRenderOnlyResult renderFrameOnly(int tMs, int generation) override;
    NativePreviewResult presentRendered(
        const NativePreviewTarget &target,
        int generation,
        int tMs
    ) override;

    std::shared_ptr<D2DDeviceResources> sharedDeviceResources() const noexcept;
    void cancelRealizationPrewarm();
    void waitForRealizationPrewarm();
    void adoptSharedGlyphResources(const Direct2DGpuBackend &source);

    // ---- realization「全池同步门」（2026-10 G5 多 worker 抖动修复 b 方案）----
    // 池在每次提交渲染任务前刷新：任一在岗 worker 预热未完成则全池走原路径
    // 直描，全员完成后一起切换到 realization 网格。渲染侧读 realizationReady，
    // 池侧调 refreshRealizationPoolReady 更新。
    bool realizationPrewarmComplete() const noexcept;
    bool realizationPoolReady() const noexcept;
    bool realizationPathReady() const noexcept override;
    void setRealizationPoolReady(bool ready) noexcept;

private:
    // frameStoreIndex >= 0 时渲染进帧仓槽（G6 直画），否则进共享 scratch
    // 目标（G5 回读路径）。见 Impl::frameStore 的注释。
    ProbeResult renderFrameInternal(
        int tMs,
        bool compactBands,
        bool readback,
        int frameStoreIndex = -1
    );
    // ---- G6 直画帧仓（Impl::frameStore）的策略体，全在主协议线程执行 ----
    // 取槽：同 (generation, tMs) 复用（暂停态重复渲同帧）、空闲优先、
    // 池满丢最久未用的（时间策略，不做 frame_index 取模撞槽）。
    int acquireFrameStoreSlot(int generation, std::int64_t tMs);
    // 渲染成功后登记身份；acquire 时已把旧登记清掉（渲染中途抛错则槽空闲）。
    void registerFrameStoreSlot(int index, int generation, std::int64_t tMs, bool realizationPathReady);
    // configure 改场景/改色：登记全部作废（纹理按尺寸决定是否保留）。
    void clearFrameStoreRegistrations();
    void releaseFrameStoreTextures();
    // 渲染/present 遇到更新代际：旧代整仓释放（seek/样式改动后不压显存）。
    void purgeForeignFrameGenerations(int generation);

    struct Impl;
    D2DDevice device_;
    std::unique_ptr<Impl> impl_;
    NativePreviewSurface previewSurface_;
};

}  // namespace krok::subtitle::native
