const pptxgen = require('pptxgenjs');
const html2pptx = require('./html2pptx.js');
const path = require('path');

async function main() {
  const pptx = new pptxgen();
  pptx.layout = 'LAYOUT_16x9';
  pptx.author = 'AI Voice Memo Prototype';
  pptx.title = 'AIボイスメモ – プロトタイプ紹介';

  const slidesDir = path.resolve(__dirname, 'slides');
  const files = [
    '01_title.html',
    '02_problem.html',
    '03_approach.html',
    '04_architecture.html',
    '05_multisource.html',
    '06_edit.html',
    '07_output.html',
    '07b_ui_tour.html',
    '08_stack.html',
    '09_next.html',
  ];

  const imgDir = path.resolve(__dirname, 'images');
  for (const f of files) {
    const p = path.join(slidesDir, f);
    console.log('Processing:', f);
    const { slide, placeholders } = await html2pptx(p, pptx);
    if (f === '07b_ui_tour.html' && placeholders.length >= 2) {
      const mob = placeholders.find(x => x.id === 'ph-mob') || placeholders[0];
      const adm = placeholders.find(x => x.id === 'ph-adm') || placeholders[1];
      slide.addImage({ path: path.join(imgDir, 'mobile_cropped.png'),
        x: mob.x, y: mob.y, w: mob.w, h: mob.h });
      slide.addImage({ path: path.join(imgDir, 'admin_cropped.png'),
        x: adm.x, y: adm.y, w: adm.w, h: adm.h });
    }
  }

  const out = path.resolve(__dirname, '..', 'pitch_ai_voice_memo.pptx');
  await pptx.writeFile({ fileName: out });
  console.log('Wrote:', out);
}

main().catch(err => {
  console.error('FAILED:', err);
  process.exit(1);
});
