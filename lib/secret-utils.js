const { createHash, timingSafeEqual } = require('node:crypto');

function matchesSecret(provided, expected) {
  if (!provided || !expected) return false;
  const digest = value => createHash('sha256').update(value).digest();
  return timingSafeEqual(digest(provided), digest(expected));
}

module.exports = { matchesSecret };
