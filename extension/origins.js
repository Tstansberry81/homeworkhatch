// The Homework Hatch site has more than one address (Render's and the custom domain), all the same
// server. Given the manifest's externally_connectable match patterns, sameServerAs returns a test:
// is the server the extension is linked to (endpointUrl) the same as the page asking (origin)?
export function sameServerAs(matches = []) {
  const own = new Set((matches || []).map((m) => m.replace(/\/\*$/, "")));
  const originOf = (url) => { try { return new URL(url).origin; } catch { return null; } };
  return (endpointUrl, origin) => {
    const linked = originOf(endpointUrl);
    return Boolean(linked) && (linked === origin || (own.has(linked) && own.has(origin)));
  };
}
