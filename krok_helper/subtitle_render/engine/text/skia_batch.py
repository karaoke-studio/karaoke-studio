"""Deduplicated, explicit font instances shared by canonical text consumers."""
from contextlib import nullcontext
import hashlib
import json
import math


def font_snapshot(resolve_font):
    """Optional resolver capability: one physical content snapshot per preparation.

    Every batch of a canonical preparation then exports the same digest for a
    path; the next preparation rereads the content instead of reusing it.
    """
    snapshot = getattr(resolve_font, 'snapshot', None)
    return snapshot() if snapshot is not None else nullcontext()


class TextMeasurementBatch:
    def __init__(self, resolve_font):
        self.resolve_font = resolve_font
        self.fonts, self.runs = {}, []
        self._instances, self._texts = {}, []
        self._chains = {}

    def instance(self, appearance, stretch=1.0, *, text=None, body=False):
        body_discover = getattr(self.resolve_font, 'resolve_body_cell_fonts', None) if body else None
        discover = body_discover or getattr(self.resolve_font, 'resolve_cell_fonts', None)
        if discover is None:
            text = None
        key = (appearance.font_family, appearance.font_weight, appearance.italic,
               appearance.font_size_px, stretch, text, body_discover is not None)
        if key not in self._instances:
            chain = (discover(appearance, text) if text is not None and discover is not None
                     else [self.resolve_font(appearance)])
            if not chain or len(chain) > 32:
                raise ValueError('Skia 显式字体链大小无效')
            ids, instances = [], []
            for resource, options in chain:
                font_id = hashlib.sha256(json.dumps(resource, sort_keys=True).encode()).hexdigest()
                self.fonts[font_id] = resource
                ids.append(font_id)
                instances.append(dict(options, scale_x=options.get('scale_x', 1) * stretch))
            run = {'font_ids': ids, 'size': appearance.font_size_px, **instances[0]}
            if len(chain) > 1:
                run['font_options'] = instances
                if getattr(self.resolve_font, 'allow_mixed_fonts', False):
                    run['mixed_fallback'] = True
            # Different texts sharing the same explicit chain still batch once.
            chain_key = json.dumps(run, sort_keys=True)
            existing = self._chains.get(chain_key)
            if existing is None:
                existing = len(self.runs)
                self.runs.append(dict(run, cells=[]))
                self._texts.append({})
                self._chains[chain_key] = existing
            self._instances[key] = existing
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
            if len(run['font_ids']) > 1 and (len(result.get('font_chain', [])) != len(run['font_ids'])
                    or any(not isinstance(v, str) or not v for v in result['font_chain'])):
                raise ValueError('Skia 批量度量缺少完整字体链身份')
            if 'cap_height' in baseline and (not math.isfinite(float(baseline['cap_height']))
                                            or float(baseline['cap_height']) < 0):
                raise ValueError('Skia 字体大写高度包含无效或非有限值')
        return results

    def resolved_runs(self):
        return tuple({k: v for k, v in r.items() if k != 'cells'} for r in self.runs)
