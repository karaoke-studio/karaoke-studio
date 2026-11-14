#pragma once

#include "render_surface.h"

namespace krok::subtitle::native {

struct NativePreviewTarget {
    std::uintptr_t parentWindow = 0;
    // 子窗口在父窗口客户区的物理像素矩形（可能小于渲染纹理：场景映射
    // 矩形被视口裁剪）。srcX/srcY 是窗口左上角在纹理里的物理像素偏移，
    // present 时从该偏移 1:1 拷贝窗口大小的区域。
    int x = 0;
    int y = 0;
    int width = 0;
    int height = 0;
    int srcX = 0;
    int srcY = 0;
};

// G6 拆分路径第一步：只渲染到纹理（不上屏），Python 侧到点后调
// gpu_present_rendered 上屏——「渲染可提前，播放必须到点」。
struct NativePreviewResult {
    double renderMs = 0.0;
    double presentMs = 0.0;
    std::uintptr_t childWindow = 0;
    // 帧仓未命中（present 声称的 (generation, tMs) 不在仓里）：本次不上屏，
    // 调用方按丢帧处理。仓命中前绝不把「别的时刻的像素」端出去——G6 单
    // 纹理时代 present 无视 t_ms 直接拷最新渲染结果，是慢机预览回退的
    // 根因（2026-10）。
    bool dropped = false;
    bool realizationPathStale = false;
    // 上屏像素的路径与资源版本（帧仓登记值时记录）：present 成功后据此把
    // 画面变化与路径/资源换代对上号。
    bool realizationPathReady = false;
    std::uint64_t realizationGeneration = 0;
};
struct NativeRenderOnlyResult {
    double renderMs = 0.0;
    // 本帧的路径 / 资源版本诊断（直画路径也要能回答「这一帧是谁画的、
    // 用的哪一代资源」——抖动定位闭环，2026-10）。
    ProbeResult::FrameDiagnostics frameDiagnostics;
};

}  // namespace krok::subtitle::native
