import { Canvas, cn } from "@unity/canvas-kit";

type Invoice = {
  reference: string;
  customer: string;
  amount: number;
  currency: string;
  days_overdue: number;
  stage: string;
  chases_sent: number;
  last_chase_at: string;
  status: string;
};

// The ladder, in the order it escalates. Rendered as a role token rather
// than a colour so the same view reads correctly in either theme.
const STAGE_TONE: Record<string, string> = {
  reminder: "text-muted-foreground",
  follow_up: "text-foreground",
  firm: "text-destructive",
};

function money(amount: number, currency: string): string {
  const value = Number.isFinite(amount) ? amount : 0;
  return `${currency || ""} ${value.toFixed(2)}`.trim();
}

export default function View({ canvas }: { canvas: { invoices?: Invoice[] } }) {
  const invoices = canvas?.invoices ?? [];
  const outstanding = invoices.reduce(
    (total, invoice) => total + (Number.isFinite(invoice.amount) ? invoice.amount : 0),
    0,
  );
  const currency = invoices[0]?.currency ?? "";
  const oldest = invoices.reduce(
    (worst, invoice) => Math.max(worst, invoice.days_overdue ?? 0),
    0,
  );

  return (
    <Canvas>
      <div className="flex flex-col gap-4">
        <div className="flex flex-wrap gap-6">
          <div className="flex flex-col">
            <span className="text-xs uppercase text-muted-foreground">Outstanding</span>
            <span className="text-2xl font-semibold">{money(outstanding, currency)}</span>
          </div>
          <div className="flex flex-col">
            <span className="text-xs uppercase text-muted-foreground">Invoices</span>
            <span className="text-2xl font-semibold">{invoices.length}</span>
          </div>
          <div className="flex flex-col">
            <span className="text-xs uppercase text-muted-foreground">Oldest</span>
            <span className="text-2xl font-semibold">{oldest} days</span>
          </div>
        </div>

        {invoices.length === 0 ? (
          <p className="text-sm text-muted-foreground">
            Nothing is past due. This fills as the daily sweep finds overdue invoices.
          </p>
        ) : (
          <div className="flex flex-col gap-2">
            {invoices.map((invoice) => (
              <div
                key={invoice.reference}
                className="flex items-center justify-between gap-4 rounded-lg border p-3"
              >
                <div className="flex min-w-0 flex-col">
                  <span className="truncate font-medium">{invoice.customer}</span>
                  <span className="text-xs text-muted-foreground">
                    {invoice.reference} · {invoice.days_overdue} days overdue
                  </span>
                </div>
                <div className="flex flex-col items-end">
                  <span className="font-medium">
                    {money(invoice.amount, invoice.currency)}
                  </span>
                  <span
                    className={cn(
                      "text-xs",
                      STAGE_TONE[invoice.stage] ?? "text-muted-foreground",
                    )}
                  >
                    {invoice.stage} · {invoice.chases_sent} chased
                  </span>
                </div>
              </div>
            ))}
          </div>
        )}
      </div>
    </Canvas>
  );
}
