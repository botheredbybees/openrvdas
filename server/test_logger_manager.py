#!/usr/bin/env python3

import json
import logging
import sys
import tempfile
import threading
import time
import unittest
import warnings

from os.path import dirname, realpath; sys.path.append(dirname(dirname(realpath(__file__))))

from logger.readers.text_file_reader import TextFileReader
from server.logger_manager import LoggerManager, SupervisorConnector

from server.in_memory_server_api import InMemoryServerAPI
from server.server_api_command_line import ServerAPICommandLine


sample_data = [
  'Permission is hereby granted, free of charge, to any person obtaining a copy',
  'of this software and associated documentation files (the "Software"), to deal',
  'in the Software without restriction, including without limitation the rights',
  'to use, copy, modify, merge, publish, distribute, sublicense, and/or sell',
  'copies of the Software, and to permit persons to whom the Software is',
  'furnished to do so, subject to the following conditions:'
]

sample_cruise = {
  "cruise": {
    "id": "NBP1700",
    "start": "2017-01-01",
    "end": "2017-02-01"
  },
  "loggers": {
    "sample": {
      "configs": ["off", "sample->file"]
    },
  },
  "modes": {
    "off": {
      "sample": "off",
    },
    "port": {
      "sample": "sample->file"
    }
  },

  "default_mode": "off",
  
  "configs": {
    "off": {},
    "sample->file": {
      "name": "sample->file",
      "readers": {
        "class": "TextFileReader",
        "kwargs": {"file_spec": "fill this in" }
      },
      "transforms": {
        "class": "PrefixTransform",
        "kwargs": {"prefix": "TestLoggerManager"}
      },
      "writers": {
        "class": "TextFileWriter",
        "kwargs": {"filename": "fill this in"}
      }
    },
  },
}


################################################################################
class TestLoggerManagerAPI(unittest.TestCase):
  ############################
  def setUp(self):
    warnings.simplefilter("ignore", ResourceWarning)

    self.tmpdir = tempfile.TemporaryDirectory()
    self.tmpdirname = self.tmpdir.name
    logging.info('created temporary directory "%s"', self.tmpdirname)

    self.input_filename = self.tmpdirname + '/input.txt'
    self.output_filename = self.tmpdirname + '/output.txt'
    self.cruise_filename = self.tmpdirname + '/cruise.yaml'

    sample_cruise['configs']['sample->file']['readers']['kwargs']['file_spec'] \
      = self.input_filename
    sample_cruise['configs']['sample->file']['writers']['kwargs']['filename'] \
      = self.output_filename

    logging.info('Creating temp input file %s', self.input_filename)
    with open(self.input_filename, 'w') as f:
      for line in sample_data:
        f.write(line + '\n')

    logging.info('Creating temp cruise file %s', self.cruise_filename)
    with open(self.cruise_filename, 'w') as f:
      f.write(json.dumps(sample_cruise, indent=4))
    
  ############################
  def test_basic(self):

    ###############################
    # Create LoggerManager and test it
    api = InMemoryServerAPI()
    supervisor = SupervisorConnector(
      start_supervisor_in=self.tmpdirname + '/supervisor',
      supervisor_logfile_dir=self.tmpdirname,
      max_tries=1,
      log_level=logging.WARNING)
    logger_manager = LoggerManager(api=api, supervisor=supervisor,
                                   logger_log_level=logging.WARNING)

    ############################
    def run_commands(logger_manager):
      api = logger_manager.api
      time.sleep(1)

      runner = ServerAPICommandLine(api)

      runner.process_command('load_configuration %s' % self.cruise_filename)
      runner.process_command('set_active_mode port')

      # Give it time to run
      time.sleep(2)

      reader = TextFileReader(self.output_filename)

      for i in range(4):
        result = reader.read()
        self.assertEqual(result, 'TestLoggerManager ' + sample_data[i])
      logging.info('TextFileReader done')
      logger_manager.quit()

    cmd_thread = threading.Thread(name='run_commands',
                                  target=run_commands, daemon=True,
                                  args=(logger_manager,))
    cmd_thread.start()
    logger_manager.start()
    cmd_thread.join()

################################################################################
if __name__ == '__main__':
  import argparse
  parser = argparse.ArgumentParser()
  parser.add_argument('-v', '--verbosity', dest='verbosity',
                      default=0, action='count',
                      help='Increase output verbosity')
  args = parser.parse_args()

  LOGGING_FORMAT = '%(asctime)-15s %(filename)s:%(lineno)d %(message)s'
  LOG_LEVELS = {0:logging.WARNING, 1:logging.INFO, 2:logging.DEBUG}
  logging.basicConfig(format=LOGGING_FORMAT)
  args.verbosity = min(args.verbosity, max(LOG_LEVELS))
  logging.getLogger().setLevel(LOG_LEVELS[args.verbosity])

  #logging.getLogger().setLevel(logging.DEBUG)
  unittest.main(warnings='ignore')
