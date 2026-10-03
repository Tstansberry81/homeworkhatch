// Answer checking for Learn mode: the browser's copy of normalize / check_answer in
// services/learn.py, so Learn (graded here) and Test (graded on the server) agree. The marks
// lenient mode ignores aren't written here: the page passes learn.IGNORED_MARKS in, so the two
// copies can't drift apart. tests/test_learn.py runs this file under node on the same vectors.
(function (root, factory) {
  const api = factory();
  if (typeof module === "object" && module.exports) module.exports = api;
  else root.hhAnswers = api;
})(typeof self !== "undefined" ? self : this, () => {
  const SOFT = /[.,;:!?'"`()[\]{}…–—‘’“”«»¿¡$\\*_~#]/g;
  const NUM = /^([-+−]?)(\$?)(\d{1,3}(?:,\d{3})+|\d+)(\.\d+)?(%?)$/;

  // [[lo, hi], ...] inclusive code point ranges -> one character class.
  const marksPattern = (ranges) => {
    const parts = (ranges || []).map(([lo, hi]) => `\\u{${Number(lo).toString(16)}}-\\u{${Number(hi).toString(16)}}`);
    return parts.length ? new RegExp(`[${parts.join("")}]`, "gu") : null;
  };

  function checker(ignoredMarks) {
    const marks = marksPattern(ignoredMarks);
    // Lenient: ignore case, spaces, punctuation and accents. Strict: only case and spacing.
    const normalize = (text, strict) => {
      let s = String(text ?? "").normalize("NFC");
      if (strict) return s.trim().split(/\s+/).filter(Boolean).join(" ").toLowerCase();
      s = s.normalize("NFKD");
      if (marks) s = s.replace(marks, "");
      s = s.normalize("NFC").toLowerCase(); // recompose what's left (が stays が)
      return s.replace(/(\d)\.(?=\d)/g, "$1\u0000").replace(SOFT, "").replace(/\u0000/g, ".").replace(/\s+/g, "");
    };
    const isNumber = (text) => NUM.test(String(text ?? "").trim());
    const oneEditApart = (a, b) => {
      if (a === b || Math.abs(a.length - b.length) > 1) return false;
      if (a.length > b.length) [a, b] = [b, a];
      let i = 0;
      while (i < a.length && a[i] === b[i]) i++;
      if (a.length !== b.length) return a.slice(i) === b.slice(i + 1);
      if (a.slice(i + 1) === b.slice(i + 1)) return true;
      return i + 1 < a.length && a[i] === b[i + 1] && a[i + 1] === b[i] && a.slice(i + 2) === b.slice(i + 2); // swapped pair
    };
    const check = (given, expected, strict) => {
      const g = normalize(given, strict), e = normalize(expected, strict);
      if (!g) return "wrong";
      if (g === e) return "right";
      if (!strict && e.length >= 4 && !isNumber(expected) && oneEditApart(g, e)) return "almost";
      return "wrong";
    };
    return { normalize, isNumber, oneEditApart, check };
  }

  return { checker };
});
