#!/bin/sh
   set -e

   cd bot-prodazh
   pip install -r requirements.txt
   python main_bot.py
