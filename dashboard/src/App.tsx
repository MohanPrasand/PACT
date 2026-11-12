import { useHashRoute } from "./lib/useHashRoute";
import { AdminApp } from "./admin/AdminApp";
import { ShopApp } from "./shop/ShopApp";

/** Two clearly separated areas: `#/shop/...` (public storefront) and `#/admin/...` (PACT dashboard). */
export default function App() {
  const route = useHashRoute();
  if (route.startsWith("/admin")) return <AdminApp route={route} />;
  if (route.startsWith("/shop")) return <ShopApp route={route} />;
  window.location.replace("#/shop");
  return null;
}
