"""Deduplicated, explicit font instances shared by canonical text consumers."""
import hashlib
import json
import math


class TextMeasurementBatch:
    def __init__(self, resolve_font):
        self.resolve_font = resolve_font
        self.fonts, self.runs = {}, []
        self._instances, self._texts = {}, []

    def instance(self, appearance, stretch=1.0):
        key = (appearance.font_family, appearance.font_weight, appearance.italic,
               appearance.font_size_px, stretch)
        if key not in self._instances:
            resource, options = self.resolve_font(appearance)
            font_id = hashlib.sha256(json.dumps(resource, sort_keys=True).encode()).hexdigest()
            self.fonts[font_id] = resource
            options = dict(options, scale_x=options.get('scale_x', 1) * stretch)
            self._instances[key] = len(self.runs)
            self.runs.append({'font_ids': [font_id], 'size': appearance.font_size_px,
                              'cells': [], **options})
            self._texts.append({})
        return self._instances[key]

    def cell(self, run, text):
        if text not in self._texts[run]:
            self._texts[run][text] = len(self.runs[run]['cells'])
            self.runs[run]['cells'].append(text)
        return self._texts[run][text]

    def measure(self, measure):
        results = measure(self.fonts, self.runs) if self.runs else []
        if len(results) != len(self.runs):
            raise ValueError('Skia 批量度量字体实例数量失配')
        for run, result in zip(self.runs, results, strict=True):
            baseline = result.get('font_metrics')
            if not isinstance(baseline, dict):
                raise ValueError('Skia sidecar 缺少字体基线度量，请重新构建')
            if (len(result['cells']) != len(run['cells']) or not baseline.get('font_identity')
                    or not all(math.isfinite(float(baseline[k])) for k in ('ascent', 'descent', 'leading'))):
                raise ValueError('Skia 批量度量单元或主字基线无效')
            if 'cap_height' in baseline and (not math.isfinite(float(baseline['cap_height']))
                                            or float(baseline['cap_height']) < 0):
                raise ValueError('Skia 字体大写高度包含无效或非有限值')
        return results

    def resolved_runs(self):
        return tuple({k: v for k, v in r.items() if k != 'cells'} for r in self.runs)
