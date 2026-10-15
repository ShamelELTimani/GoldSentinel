const token = () => localStorage.getItem('gs-token') || 'change-me'

export async function api<T>(path: string, init?: RequestInit): Promise<T> {
  const response = await fetch(path, { ...init, headers: { 'Content-Type': 'application/json', 'X-API-Token': token(), ...(init?.headers || {}) } })
  if (!response.ok) {
    const data = await response.json().catch(() => ({}))
    throw new Error(data.detail || `${response.status} ${response.statusText}`)
  }
  return response.json() as Promise<T>
}

