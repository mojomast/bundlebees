from pathlib import Path
from PIL import Image,ImageDraw
r=Path(__file__).resolve().parents[1];files=sorted((r/'previews/stills').glob('*.png'),key=lambda p:float(p.stem));w=480;h=270;gap=22;cols=3;rows=(len(files)+cols-1)//cols
sheet=Image.new('RGB',(cols*w,rows*(h+gap)),(9,15,19));d=ImageDraw.Draw(sheet)
for i,p in enumerate(files):
 x=(i%cols)*w;y=(i//cols)*(h+gap);im=Image.open(p).convert('RGB').resize((w,h),Image.Resampling.LANCZOS);sheet.paste(im,(x,y));d.text((x+8,y+h+4),p.stem+' s',fill=(255,190,79))
sheet.save(r/'previews/contact-sheet.jpg',quality=92)
