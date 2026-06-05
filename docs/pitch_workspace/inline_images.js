const fs = require('fs');
const path = require('path');

const imgDir = path.resolve(__dirname, 'images');
const slidePath = path.resolve(__dirname, 'slides', '07b_ui_tour.html');

const mob = fs.readFileSync(path.join(imgDir, 'mobile_cropped.png'));
const adm = fs.readFileSync(path.join(imgDir, 'admin_cropped.png'));

const mobUri = 'data:image/png;base64,' + mob.toString('base64');
const admUri = 'data:image/png;base64,' + adm.toString('base64');

let html = fs.readFileSync(slidePath, 'utf8');
html = html.replace(/src="mobile_cropped\.png"/g, `src="${mobUri}"`);
html = html.replace(/src="admin_cropped\.png"/g, `src="${admUri}"`);
fs.writeFileSync(slidePath, html);
console.log('inlined data URIs into 07b_ui_tour.html');
