import { Card } from "../shared/ui";
import { RequestPanel } from "./RequestPanel";

export function RequestPage({ id }: { id: string }) {
  return (
    <div className="space-y-3">
      <a href="#/admin/approvals" className="text-sm text-indigo-600 hover:underline">
        ← Approval Centre
      </a>
      <Card>
        <RequestPanel key={id} requestId={id} />
      </Card>
    </div>
  );
}
