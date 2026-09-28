Test photographs for the orientation detector
=============================================

portrait.jpg
    Neil Armstrong's NASA portrait, 1969. A real photograph of a person,
    framed the way photographs are: head and shoulders, the face high in the
    frame, background around it.

    The orientation tests need a real photograph because a Haar cascade keys
    off photographic texture — a face drawn in code is simply not detected,
    and a test built on one would pass or fail for reasons with nothing to do
    with Ninaivu.

    Why the framed version rather than the head crop: with framing around the
    head, the cascade finds the face only the right way up, so the rotation
    tests have a verdict to check. Cropped to the head alone (see face.jpg)
    that stops being true — which is exactly what that fixture is for.

    Source: Wikimedia Commons, "Neil Armstrong pose.jpg"
    https://commons.wikimedia.org/wiki/File:Neil_Armstrong_pose.jpg
    Public domain (a work of NASA, a US federal agency).

face.jpg
    A tight, centred head crop taken from portrait.jpg — the hardest case for
    a Haar cascade, and the one Ninaivu refuses: the same head is found upside
    down at almost the same size, with no framing to break the tie, so the
    honest answer is "no opinion" rather than a coin toss.
