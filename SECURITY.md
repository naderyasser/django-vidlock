# Security

vidlock is not DRM, and the README says what it does and does not stop. A
report is still very welcome when it shows that:

* a token opens a playlist or key it should not (another viewer, another
  video, after expiry, after access was revoked, a web token without its
  session);
* the key or the unencrypted source can be reached without a token;
* the fetch limit can be bypassed without a new token each time.

Please report privately through GitHub's **Report a vulnerability** button
(Security tab) rather than a public issue. You will get a reply within a few
days, and credit in the release notes if you want it.
