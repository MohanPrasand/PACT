import { Shell } from "../shared/Shell";
import { matchRoute } from "../lib/useHashRoute";
import { Card } from "../shared/ui";
import { CartPage } from "./CartPage";
import { Catalogue } from "./Catalogue";
import { OrderPage } from "./OrderPage";
import { OrdersPage } from "./OrdersPage";
import { ProductPage } from "./ProductPage";

function page(route: string) {
  const path = route.split("?")[0];
  const q = new URLSearchParams(route.split("?")[1] ?? "");
  let m: Record<string, string> | null;
  if ((m = matchRoute("/shop/products/:id", path))) return <ProductPage id={m.id} />;
  if ((m = matchRoute("/shop/cart/:cartId", path))) return <CartPage routeId={m.cartId} />;
  if ((m = matchRoute("/shop/orders/:id", path))) return <OrderPage id={m.id} start={q.get("start") === "1"} />;
  if (matchRoute("/shop/orders", path)) return <OrdersPage />;
  if (matchRoute("/shop", path)) return <Catalogue />;
  return <Card><p className="text-sm text-slate-500">Page not found. <a className="text-teal-700 underline" href="#/shop">Back to the catalogue</a>.</p></Card>;
}

export function ShopApp({ route }: { route: string }) {
  return (
    <Shell
      tone="shop"
      brand="Merchant Lab"
      tagline="Simulated store for the PACT demo"
      route={route}
      nav={[
        { href: "#/shop", label: "Catalogue", match: "/shop/products" },
        { href: "#/shop/cart/my", label: "Cart", match: "/shop/cart" },
        { href: "#/shop/orders", label: "Orders", match: "/shop/orders" },
      ]}
    >
      <div className="mb-5 rounded-md border border-teal-200 bg-teal-50 px-4 py-2 text-sm text-teal-900">
        Merchant Lab — simulated store for the PACT demo. Payments are authorised by PACT; the payment provider (PayPal Sandbox or a labelled simulation) is shown on every order.
      </div>
      {page(route)}
    </Shell>
  );
}
