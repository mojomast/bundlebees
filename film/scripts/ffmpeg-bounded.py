#!/usr/bin/env python3
import os,sys,shutil
binary=shutil.which('ffmpeg');assert binary and os.path.realpath(binary)!=os.path.realpath(__file__)
args=sys.argv[1:];out=['-threads','2','-filter_threads','2','-filter_complex_threads','2'];i=0
while i<len(args):
 if args[i] in ('-threads','-filter_threads','-filter_complex_threads'):out.extend([args[i],'2']);i+=2;continue
 if args[i]=='-i':out.extend(['-threads','2'])
 out.append(args[i]);i+=1
os.execv(binary,[binary,*out])
