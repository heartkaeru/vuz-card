import { handleAuth } from "@/lib/auth-route";

export function POST(request: Request) {
  return handleAuth(request, "login");
}
