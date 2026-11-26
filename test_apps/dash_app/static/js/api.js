import axios from "axios";

export async function loadStats() {
  const res = await axios.get("/api/stats");
  return res.data;
}

export function addItem(item) {
  return axios.post("/api/items", item);
}

export function removeItem(id) {
  return axios.delete(`/api/items/${id}`);
}
