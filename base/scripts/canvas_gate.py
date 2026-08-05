"""Build-time proof that this image can author a canvas.

Runs as the image's final layer, as the runtime user. Each check maps to a
failure that otherwise surfaces only in production:

- toolchain missing        -> every create_view fails at the build gate
- host or chromium missing -> the render review reports "skipped" and
                              unreviewed canvases publish silently
- .builds not writable     -> every compile dies at mkdtemp as uid 1000

The probe canvas exercises the real lint -> tsc -> esbuild chain the way a
canvas is actually authored: the kit supplies only the protocol layer, and the
presentational parts are inlined shadcn source compiled against the vendored
substrate. That shape is what makes this a proof — a substrate package missing
from the image (radix, cva, lucide, recharts), a stylesheet whose class
manifest was not copied, or a kit/toolchain version mismatch each fail here
rather than on the first live publish.
"""

from unify.canvas_manager.ops.build_ops import build_canvas, toolchain_available
from unify.canvas_manager.ops.review_ops import (
    _browser_available,
    _host_root,
    render_and_review,
)

TSX = """
import * as React from 'react';
import { cva, type VariantProps } from 'class-variance-authority';
import { Activity } from 'lucide-react';
import { Line, LineChart, XAxis } from 'recharts';
import { Canvas, cn, seriesColor, type CanvasViewProps } from '@unity/canvas-kit';

const badgeVariants = cva(
  'inline-flex items-center rounded-md border px-2 py-0.5 text-xs font-medium',
  {
    variants: {
      variant: {
        default: 'border-transparent bg-primary text-primary-foreground',
        secondary: 'border-transparent bg-secondary text-secondary-foreground',
      },
    },
    defaultVariants: { variant: 'default' },
  },
);

function Badge({
  className,
  variant,
  ...props
}: React.ComponentProps<'span'> & VariantProps<typeof badgeVariants>) {
  return <span className={cn(badgeVariants({ variant }), className)} {...props} />;
}

function Card({ className, ...props }: React.ComponentProps<'div'>) {
  return (
    <div
      className={cn('rounded-xl border bg-card text-card-foreground shadow-sm', className)}
      {...props}
    />
  );
}

const SERIES = [
  { day: 'Mon', value: 4 },
  { day: 'Tue', value: 7 },
  { day: 'Wed', value: 5 },
];

export default function Probe({ canvas }: CanvasViewProps) {
  const note = String(canvas.props.note ?? 'ok');
  return (
    <Canvas>
      <Card className="flex flex-col gap-4 p-6">
        <div className="flex items-center gap-2">
          <Activity className="h-4 w-4 text-muted-foreground" />
          <span className="text-sm font-medium">Gate</span>
          <Badge variant="secondary">{note}</Badge>
        </div>
        <LineChart
          width={360}
          height={120}
          data={SERIES}
          margin={{ top: 8, right: 24, bottom: 0, left: 24 }}
        >
          <XAxis dataKey="day" />
          <Line
            dataKey="value"
            stroke={seriesColor(0)}
            strokeWidth={2}
            isAnimationActive={false}
          />
        </LineChart>
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
assert code, "probe produced no bundle"
# Every specifier the host resolves through its import map must survive the
# compile as a bare import. If the externals list goes missing, esbuild inlines
# the substrate instead: the bundle silently gains hundreds of kilobytes, real
# canvases start hitting the row ceiling, and a second React breaks hooks.
for specifier in (
    "@unity/canvas-kit",
    "react",
    "recharts",
    "lucide-react",
    "class-variance-authority",
):
    assert (
        specifier in code
    ), f"probe bundle inlined {specifier} instead of importing it"
assert len(report.bundle_sha) == 64, "probe bundle has no content hash"

# Asserted separately: a missing host and a missing browser have entirely
# different fixes, and a lumped assert once cost a build cycle to find out
# which half had failed.
assert _host_root() is not None, "canvas host missing from the image"
assert _browser_available(), (
    "chromium unresolvable by the runtime user — check PLAYWRIGHT_BROWSERS_PATH "
    "and the browser install layout"
)

# Render for real, not just probe for executables: a chromium that exists but
# cannot launch, or a browser registry pointed somewhere else at runtime, both
# surface as a skip that reports rendered=True with no screenshots. Requiring
# the screenshots is what makes this a proof rather than a smoke test.
review = render_and_review(token="canvasgate01", bundle=code, props={}, rows={})
assert review.rendered, f"probe canvas did not render: {review.error}"
assert review.screenshots, (
    f"render was skipped, not performed (verdict: {review.verdict!r}) — "
    "the runtime user could not launch a browser"
)

print(
    f"canvas gate ok: {report.bytes} bytes in {report.duration_ms} ms, "
    f"{len(review.screenshots)} screenshots",
)
