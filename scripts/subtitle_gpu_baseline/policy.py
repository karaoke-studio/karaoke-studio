"""Initial review budgets, fixed before a Skia implementation is available.

These are comparison gates, not measured Skia results. Geometry/timing assertions
remain mandatory even when a full-image budget passes. Existing D2D deviations
never silently relax the same-backend byte-identity requirement.
"""

COMPARISON_BUDGETS = {
    "schema": 1,
    "same_backend": {"byte_identical": True, "requires_stable_render_path": True},
    "semantic": {"time_ms_exact": True, "page_lane_exact": True,
        "glyph_position_logical_px": 0.25, "particle_xy_px_at_1080": 0.02,
        "particle_alpha_abs": 0.0001, "counts_and_seeds_exact": True},
    "image_categories": {
        "text": {"min_alpha_iou": 0.98, "max_bbox_edge_delta_px_at_1080": 1,
            "max_union_channel_mae": 3, "max_union_p95": 16, "max_alpha_sum_relative": 0.02},
        "decorated_text": {"min_alpha_iou": 0.97, "max_bbox_edge_delta_px_at_1080": 2,
            "max_union_channel_mae": 5, "max_union_p95": 24, "max_alpha_sum_relative": 0.03},
        "particles": {"min_alpha_iou": 0.95, "max_bbox_edge_delta_px_at_1080": 2,
            "max_union_channel_mae": 6, "max_union_p95": 32, "max_alpha_sum_relative": 0.03},
    },
    "low_alpha": {"mask_threshold": 16, "minimum_union_for_iou": 64,
        "require_alpha_sum_check": True, "never_ignore_missing_positive_frame": True},
    "review": {"visual_review_required": True, "per_element_geometry_required": True,
        "threshold_change_requires_named_case_and_reason": True,
        "blank_background_is_not_accuracy_evidence": True},
}


def budget_categories(case) -> list[str]:
    result = ["text"]
    if case.style.decoration_kind != "none" or "F08" in case.features:
        result.append("decorated_text")
    if set(case.features) & {"F15", "F16", "F20"}:
        result.append("particles")
    return result
