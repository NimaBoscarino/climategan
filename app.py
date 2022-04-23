import os
os.system('mkdir config')
os.system('cd config')
os.system('gdown https://drive.google.com/u/0/uc?id=18OCUIy7JQ2Ow_-cC5xn_hhDn-Bp45N1K')
os.system('unzip release-github-v1.zip')
os.system('cd ..')
os.system('python apply_events.py -b 4 -i ../inputs -r config/model/masker --output_path ../outputs --overwrite')

