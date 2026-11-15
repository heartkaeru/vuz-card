import { NextResponse, type NextRequest } from "next/server";

const ACCESS_TOKEN_COOKIE = "vuz_access_token";

export function proxy(request: NextRequest) {
  const { pathname } = request.nextUrl;

  if (pathname.startsWith("/api/")) {
    const requestHeaders = new Headers(request.headers);
    const token = request.cookies.get(ACCESS_TOKEN_COOKIE)?.value;
    if (token) requestHeaders.set("Authorization", `Bearer ${token}`);
    return NextResponse.next({ request: { headers: requestHeaders } });
  }

  const hasSession = Boolean(request.cookies.get(ACCESS_TOKEN_COOKIE)?.value);
  if (pathname === "/login") {
    return hasSession ? NextResponse.redirect(new URL("/", request.url)) : NextResponse.next();
  }
  if (hasSession) return NextResponse.next();

  const loginUrl = new URL("/login", request.url);
  loginUrl.searchParams.set("next", `${pathname}${request.nextUrl.search}`);
  return NextResponse.redirect(loginUrl);
}

export const config = {
  matcher: [
    "/api/:path*",
    "/((?!_next/static|_next/image|favicon.ico|.*\\..*).*)",
  ],
};
