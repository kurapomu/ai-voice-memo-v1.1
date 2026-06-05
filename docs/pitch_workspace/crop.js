const sharp = require('sharp');
const path = require('path');

async function main() {
  const imgDir = path.resolve(__dirname, 'images');

  // モバイル: 上部の余白を少しトリム
  const mob = sharp(path.join(imgDir, 'mobile_recording.png'));
  const mobMeta = await mob.metadata();
  console.log('mobile:', mobMeta.width, 'x', mobMeta.height);
  await sharp(path.join(imgDir, 'mobile_recording.png'))
    .extract({ left: 0, top: 0, width: mobMeta.width, height: Math.min(mobMeta.height, Math.floor(mobMeta.width * 1.95)) })
    .resize({ width: 460 })
    .toFile(path.join(imgDir, 'mobile_cropped.png'));

  // 管理画面: 一覧表示部分。左サイドバー＋メイン中央付近のキャプションを残す
  const adm = sharp(path.join(imgDir, 'admin_top.png'));
  const admMeta = await adm.metadata();
  console.log('admin:', admMeta.width, 'x', admMeta.height);
  // 横方向は 0..1100 ぐらいに切って、縦は 0..760
  await sharp(path.join(imgDir, 'admin_top.png'))
    .extract({ left: 0, top: 0, width: Math.min(admMeta.width, 1200), height: Math.min(admMeta.height, 760) })
    .resize({ width: 1200 })
    .toFile(path.join(imgDir, 'admin_cropped.png'));

  console.log('cropped saved');
}

main().catch(e => { console.error(e); process.exit(1); });
