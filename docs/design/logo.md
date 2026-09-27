# CSUPOR logo

The project owner's supplied artwork is a terracotta pot containing a calendar, with the **CSUPOR** wordmark beneath. The portal uses a transparent version in the navigation, public header, sign-in and registration panels. The browser tab uses the same artwork.

- `app/static/images/csupor-logo.webp`: 512-pixel web asset, with alpha transparency.
- `app/static/images/csupor-favicon.png`: 96 × 96 transparent browser icon.
- The images are served locally and have explicit display dimensions and accessible text. The decorative authentication illustration has an empty alternative text to avoid repeating the brand name.

The built-in Imagegen editor prepared the transparent asset from the supplied image. Its editing prompt requested removal of the white rounded card, grey backdrop, card shadow and outer padding, while preserving the pot, calendar, rays, light grounding shadow and exact **CSUPOR** lettering, colours and stacked arrangement. The resulting image was exported to the web formats above with FFmpeg. No runtime image processing or new application dependency is needed.

Verification: 24 Chromium page/locale/viewport checks covering login, registration and dashboard in English and Hungarian at 320, 390, 768 and 1440 pixels. Images and favicon loaded successfully; the mobile menu, its close button and Escape behaviour worked, with no document overflow or JavaScript errors.
