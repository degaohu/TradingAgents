// Standalone script that just hosts the JobRoom Durable Object class.
// Re-exports from the API worker's source to avoid duplicating the class.
export { JobRoom } from "../../api/src/durable/job-room";

export default {
  async fetch(): Promise<Response> {
    return new Response("job-room script — DO-only", { status: 404 });
  },
};
