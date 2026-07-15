/**
 * Placeholder routes — filled in during MVP week 3.
 * The stubs return 501 so the SPA can call them and see them clearly
 * not-yet-implemented instead of erroring or 404ing.
 */

import { Hono } from "hono";
import type { AppContext } from "../index";

const app = new Hono<AppContext>();

app.all("*", (c) => c.json({ error: "not_implemented", route: c.req.path }, 501));

export default app;
