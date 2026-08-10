import { state } from "./core.js";

async function postEventStream(path, body, signal) {
  return fetch(path, {
    method: "POST",
    headers: {
      Accept: "text/event-stream",
      "Content-Type": "application/json",
      "X-ACM-Token": state.token,
    },
    body: JSON.stringify(body),
    signal,
  });
}

export { postEventStream };
