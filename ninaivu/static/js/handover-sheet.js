/**
 * The printed handover sheet's one button.
 *
 * The sheet is a page of its own (api/api_handover.py) and the policy allows
 * no script written into a page, so the Print button is wired from here.
 * Hidden when printed, by the sheet's own print styles.
 */
const button = document.getElementById('print-sheet');
if (button) button.addEventListener('click', () => window.print());
