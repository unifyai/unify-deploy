"""Build-time proof that this image can author a canvas.

Runs as the image's final layer, as the runtime user. Each check maps to a
failure that otherwise surfaces only in production:

- toolchain missing        -> every create_view fails at the build gate
- host or chromium missing -> the render review reports "skipped" and
                              unreviewed canvases publish silently
- .builds not writable     -> every compile dies at mkdtemp as uid 1000

The probe canvas exercises the real lint -> tsc -> esbuild chain against the
vendored kit, so a kit/toolchain version mismatch also fails here rather than
on the first live publish.
"""

from unify.canvas_manager.ops.build_ops import build_canvas, toolchain_available
from unify.canvas_manager.ops.review_ops import gate_available

TSX = """
import { Canvas, Card, CardContent, CardHeader, CardTitle, type CanvasViewProps } from '@unity/canvas-kit';

export default function Probe({ canvas }: CanvasViewProps) {
  const note = String(canvas.data.note ?? 'ok');
  return (
    <Canvas>
      <Card>
        <CardHeader>
          <CardTitle>Gate</CardTitle>
        </CardHeader>
        <CardContent>{note}</CardContent>
      </Card>
    </Canvas>
  );
}
"""

assert toolchain_available(), "canvas toolchain missing from the image"

report, code = build_canvas(TSX, kit_version="gate")
assert (
    report.ok
), f"canvas probe failed at {report.failed_stage}: {'; '.join(report.diagnostics)}"
assert (
    code and "@unity/canvas-kit" in code
), "probe bundle did not keep the kit external"
assert len(report.bundle_sha) == 64, "probe bundle has no content hash"

assert gate_available(), "canvas host or chromium unavailable to the runtime user"

print(f"canvas gate ok: {report.bytes} bytes in {report.duration_ms} ms")
