#!/usr/bin/env python3
"""Low-level class to start/stop loggers according passed in
definitions and commands.

Intended to be used in one of four ways:

(See note below about simulated serial ports if you want to try the
command lines below)

1. As a simple standalone logger runner: you give it an intial dict of
   logger configurations, and it tries to keep them running.

     server/logger_runner.py --config test/configs/sample_configs.yaml

   Note: the LoggerRunner doesn't know anything about modes, and doesn't
   take a full cruise definition - it just takes a dict of logger
   configurations and runs them.

2. As instantiated by a LoggerManager:

     server/logger_manager.py --config test/configs/sample_cruise.yaml -v

   When invoked as above, the LoggerManager will instantiate a LoggerRunner
   interpret commands from the command line and dispatch the requested
   loggers to its LoggerRunner. (Type "help" on the LoggerManager command
   line for the commands it understands.)

3. As instantiated by some other script that directly creates and
   manages a LoggerRunner object.

4. As a websocket client taking orders from a LoggerManager:

     server/run_loggers.py --websocket localhost:8765 --host_id knud.host

   with a LoggerManager running like this:

     server/logger_manager.py --websocket :8765 --host_id master.host

   When invoked with the --websocket flag, a LoggerManager will
   attempt to create a websocket server at the specified address and
   listen for clients to connect.

   When invoked with the --websocket and --host_id flags, a
   LoggerRunner will attempt to connect to the named address and
   identify itself by the provided host_id.

   When the LoggerManager identifies a config to be run that includes
   a host_id restriction, e.g.

        "knud->net": {
            "host_id": "knud.host",
            "readers": ...
            ...
         }

   it will attempt to dispatch that config to the appropriate
   client. (Note: configs with no host restriction will be run by the
   LoggerManager itself, and if a config has a host restriction and no
   host by that name has connected, the LoggerManager will issue a
   warning and not run the config.)


Simulated Serial Ports:

The sample_cruise.yaml and sample_configs.yaml files above specify
configs that read from simulated serial ports and write to UDP port
6224. To get the configs to actually run, you'll need to run

  logger/utils/serial_sim.py --config test/serial_sim.yaml

in a separate terminal window to create the virtual serial ports the
sample config references and feed simulated data through them.)

To verify that the scripts are actually working as intended, you can
create a network listener on port 6224 in yet another window:

  logger/listener/listen.py --network :6224 --write_file -
"""
import asyncio
import json
import logging
import multiprocessing
import os
import pprint
import signal
import sys
import time
import threading

from os.path import dirname, realpath; sys.path.append(dirname(dirname(realpath(__file__))))

from logger.utils.read_config import read_config
from logger.utils.stderr_logging import setUpStdErrLogging, StdErrLoggingHandler
from logger.writers.text_file_writer import TextFileWriter
from logger.listener.listen import ListenerFromLoggerConfig

############################
def kill_handler(self, signum):
  """Translate an external signal (such as we'd get from os.kill) into a
  KeyboardInterrupt, which will signal the start() loop to exit nicely."""
  logging.info('Received external kill')
  raise KeyboardInterrupt('Received external kill signal')

############################
def runnable(config):
  """Is this logger configuration runnable? (Or, e.g. does it just have
  a name and no readers/transforms/writers?)
  """
  return config and ('readers' in config or 'writers' in config)

################################################################################
class LoggerRunner:
  ############################
  def __init__(self, interval=0.5, max_tries=3, initial_configs=None,
               event_loop=None, logger_log_level=logging.WARNING):
    """Create a LoggerRunner.
    interval - number of seconds to sleep between checking/updating loggers

    max_tries - number of times to try a dead logger config. If zero, then
                never stop retrying.

    initial_configs - optional dict of configs to start up on creation.

    event_loop - Optional event loop, if we're instantiated in a thread
                that doesn't have its own.

    logger_log_level - At what logging level our component loggers
                should operate.
    """
    logging.info('Starting LoggerRunner')
    # Map logger name to config, process running it, and any errors
    self.logger_configs = {}
    self.processes = {}
    self.errors = {}
    self.num_tries = {}
    self.failed_loggers = set()

    # We want to remember that we've shut down and erased a logger so
    # that the next time we're asked for a status update, we can add
    # one last notice that it's not running. Without it, the most
    # recent status report for it will be its previous state.
    self.disappeared_loggers = set()

    self.interval = interval
    self.max_tries = max_tries
    self.logger_log_level = logger_log_level

    self.quit_flag = False

    # If we've had an event loop passed in, use it
    self.event_loop = event_loop or asyncio.get_event_loop()
    if event_loop:
      asyncio.set_event_loop(event_loop)

    # Set the signal handler so that an external break will get
    # translated into a KeyboardInterrupt. But signal only works if
    # we're in the main thread - catch if we're not, and just assume
    # everything's gonna be okay and we'll get shut down with a proper
    # "quit()" call othewise.
    try:
      signal.signal(signal.SIGTERM, kill_handler)
    except ValueError:
      logging.info('LoggerRunner not running in main thread; '
                   'shutting down with Ctl-C may not work.')

    # Don't let other threads mess with data while we're
    # messing. Re-entrant so that we don't have to worry about
    # re-acquiring when, for example set_configs() calls set_config().
    self.config_lock = threading.RLock()

    # Only let one call to check_loggers() proceed at a time
    self.check_loggers_lock = threading.Lock()

    # If we were given any initial configs, set 'em up
    if initial_configs:
      self.set_configs(initial_configs)

  ############################
  def logger_is_alive(self, logger):
    """Is the logger for the passed name alive?
    """
    process = self.processes.get(logger, None)
    return self.process_is_alive(process)

  ############################
  def process_is_alive(self, process):
    """Is the passed process alive?
    """
    return process and process.is_alive()

  ############################
  def set_configs(self, new_configs):
    """Start/stop loggers as necessary to move from current configs
    to new configs.

    new_configs - a dict of {logger_name:config} for all loggers that
                  should be running
    """
    # All loggers, whether in current configuration or desired mode.
    with self.config_lock:
      # If loggers we know about are no longer part of new config,
      # shut them down and delete them. Note that this is different
      # from the logger having an empty/None configuration, which
      # means we should shut it down, but we still remember it.
      self.disappeared_loggers = set(self.logger_configs) - set(new_configs)
      if self.disappeared_loggers:
        logging.info('New configuration contains no mention of some '
                    'loggers. Shutting down and deleting: %s',
                     self.disappeared_loggers)
        for logger in self.disappeared_loggers:
          self._kill_and_delete_logger(logger)

      # Aggregate names of old (current) configs so that we don't
      # unnecessarily start/stop a config that's where it should be.
      #old_config_names = [config.get('name', None)
      #                    for config in self.logger_configs.values()]

      # Now set all the other loggers in their new configs. This
      # includes starting them up if new config is running and
      # shutting them down if it isn't.
      for logger, config in new_configs.items():
        self.set_config(logger, config)

  ############################
  def set_config(self, logger, new_config):
    """Start/stop individual logger to put into new config.

    logger - name of logger

    new_config - dict containing Listener configuration.
    """
    with self.config_lock:
      current_config = self.logger_configs.get(logger, None)
      if new_config == current_config:
        logging.debug('Logger %s config didn\'t change. Skipping', logger)
        return

      config_name = new_config.get('name', 'Unknown') if new_config else None
      logging.info('Setting logger %s to config %s', logger, config_name)

      # Save new config and reset our various flags it *seems*
      # reasonable to reset errors and number of tries if user has
      # reset config, even if it is to same config.
      self.logger_configs[logger] = new_config
      self.num_tries[logger] = 0
      self.errors[logger] = []

      # Logger isn't running and shouldn't be running. Nothing to do here
      if not runnable(current_config) and not runnable(new_config):
        return

      # If current_config == new_config (and is not None) and the
      # process is running, all is right with the world; skip to next.
      process = self.processes.get(logger, None)
      if current_config == new_config:
        if not process:
          warning = 'No process found for "%s"' % config_name
          logging.warning(warning)
          self.errors[logger].append(warning)
        elif not self.process_is_alive(process):
          warning = 'Process for "%s" unexpectedly dead!' % config_name
          logging.warning(warning)
          self.errors[logger].append(warning)
        else:
          # Config hasn't changed, we have process and it's alive. All
          # is well - go home.
          return

      # Either old and new config don't match or process is dead. If
      # process isn't dead, then it means old and new config don't
      # match. So kill old process before starting new one.
      self._kill_logger(logger)

      # Finally, if we have a new config, start up the new process for it
      if runnable(new_config):
        logging.debug('Starting up new process for %s', config_name)
        self._start_logger(logger, new_config)
        self.num_tries[logger] = 1

  ############################
  def _start_listener_in_new_process(self, config):
    listener = ListenerFromLoggerConfig(config)
    listener.run()

  ############################
  def _process_logger_output(self, logger, stream):
    """Run in a separate thread, reading the pipes connected to the logger
    process and sending it to the python logging module.
    """
    previous_line = None
    while True:
      try:
        line = stream.readline().decode().strip()
        if line:
          line = 'Logger ' + logger + ': ' + line
      except KeyboardInterrupt:
        return

      if not line or line == previous_line:  # Only output if something new
        continue
      else:
        previous_line = line
      if line.find(' :DEBUG: ') > -1:
        logging.debug(line)
      elif line.find(' :INFO: ') > -1:
        logging.info(line)
      elif line.find(' :WARNING: ') > -1:
        logging.warning(line)
      elif line.find(' :ERROR: ') > -1:
        logging.error(line)
        self.errors[logger].append(line)
      elif line.find(' :FATAL: ') > -1:
        logging.fatal(line)
        self.errors[logger].append(line)
      else:
        sys.stderr.write(line + '\n')

  ############################
  def _start_logger(self, logger, config):
    """Create a new process running a Listener/Logger using the passed
    config, and return the Process object.
    """
    #### Convenience routine so that Listener can be created in  own process
    def _create_listener(config, logger_log_level):
      listener = ListenerFromLoggerConfig(config=config,
                                          log_level=logger_log_level)
      listener.run()

    logging.info('Starting logger %s, config: %s',
                 logger, config.get('name', 'no-name'))
    logging.debug('Starting config:\n%s', pprint.pformat(config))

    # The multiprocessing way of starting a process
    try:
      proc = multiprocessing.Process(
        target=_create_listener,
        args=(config, self.logger_log_level),
        daemon=True)
      proc.start()

    # If something went wrong. If it was a KeyboardInterrupt, signal
    # everybody to quit. Otherwise stash error and return None
    except Exception as e:
      if e is KeyboardInterrupt:
        self.quit()
        return
      logging.error('Config %s got exception: %s', config['name'], str(e))
      proc = None
      self.errors[logger].append(str(e))

    # Store the new setup (or the wreckage, depending)
    with self.config_lock:
      self.logger_configs[logger] = config
      self.processes[logger] = proc
      self.failed_loggers.discard(logger)

  ############################
  def _kill_logger(self, logger):
    """Kill the process associated with this logger and clean out
    the data associated with it."""
    with self.config_lock:
      process = self.processes.get(logger, None)
      if process:
        try:
          logging.debug('Shutting down %s (pid %d)', logger, process.pid)
          os.kill(process.pid, signal.SIGKILL)
          process.terminate()
          process.join()
        except:
          pass
      else:
        logging.debug('Attempted to kill process for %s, but no '
                     'associated process found.', logger)

    # Clean out debris from old logger process
    #self.logger_configs[logger] = None
    self.processes[logger] = None
    self.errors[logger] = []
    self.failed_loggers.discard(logger)

  ############################
  def _kill_and_delete_logger(self, logger):
    """Not only kill the logger, but remove all trace of it from memory."""
    self._kill_logger(logger)

    # Clean out debris from old logger process
    if logger in self.logger_configs: del self.logger_configs[logger]
    if logger in self.processes: del self.processes[logger]
    if logger in self.errors: del self.errors[logger]
    self.failed_loggers.discard(logger)

  ############################
  def check_logger(self, logger, manage=False, clear_errors=False):
    """Check whether passed logger is in state it should be. Restart/stop it
    as appropriate. Return True if logger is in desired state.

    logger - name of logger to check.

    manage - if True, and if logger isn't in state it's supposed to be,
             try restarting it.
    clear_errors - if True, clear out accumulated error messages.
    """
    with self.config_lock:
      config = self.logger_configs.get(logger, None)
      config_name = config.get('name', 'unknown') if config else None
      process = self.processes.get(logger, None)

      # Now figure out whether we are (and should be) running.

      # Not running and shouldn't be. We're good. Reset our warnings.
      if not runnable(config) and not process:
        running = None
        self.failed_loggers.discard(logger)
        self.errors[logger] = []
        self.num_tries[logger] = 0

      # If we are running and are supposed to be, also good.
      elif runnable(config) and self.process_is_alive(process):
        running = True
        self.failed_loggers.discard(logger)
        #stdout = process.stdout.read()
        #stderr = process.stderr.read()

      # Shouldn't be running, but is?!?
      elif not runnable(config) and process:
        running = True
        if manage:
          self._kill_logger(logger)
          process = None

      # Here if it should be running, but isn't.
      else:
        running = False
        # If we're supposed to try and manage the state ourselves,
        # restart dead logger.
        if manage:
          # If we've tried restarting max_tries times, give up and
          # consider logger to have failed.
          if logger in self.failed_loggers:
            logging.debug('Logger %s (config %s) has failed %d times; '
                          'not retrying', logger, config_name, self.max_tries)
          elif self.max_tries and self.num_tries[logger] == self.max_tries:
            self.failed_loggers.add(logger)
            logging.warning(
              'Logger %s (config %s) has failed %d times; '
              'not retrying', logger, config_name, self.max_tries)
            logging.warning('FAILED %s: errors: %s', logger, self.errors[logger])
          else:
            # If we've not used up all our tries, try starting it again
            warning = 'Process for %s (config %s) unexpectedly dead; ' \
                      'restarting' % (logger, config_name)
            logging.warning(warning)
            self.errors[logger].append(warning)
            self._start_logger(logger, self.logger_configs[logger])
            self.num_tries[logger] += 1

      status = {
        'config': config_name,
        'errors': self.errors.get(logger, []),
        'running': running,
        'failed': logger in self.failed_loggers,
        'pid': process.pid if process else None
      }

      # Clear accumulated errors for this logger if they've asked us to
      if clear_errors:
        self.errors[logger] = []
    return status

  ############################
  def check_loggers(self, manage=False, clear_errors=False):
    """Check logger status, returning a dict of

      logger_id:
          config  - name of config (or None)
          errors  - list of errors
          running - Bool whether logger process is running
          failed  - Bool whether logger process has failed
          pid:    - logger pid or None, if not running

    Parameters:
      manage - if True, try to restart/stop loggers to put them in the state
               their configs say they should be in.

      clear_errors - if True, clear out any errors reported by checking.
    """
    with self.check_loggers_lock:
      # If there are any disappeared loggers, we want to give them one
      # last status report which will show that they are not running.
      loggers = set(self.logger_configs).union(self.disappeared_loggers)

      # This is an approximation to intent: if we're clearing errors,
      # guess that we're also going to be storing statuses, so will
      # have on record that the disappeared loggers have shut down.
      if clear_errors:
        self.disappeared_loggers = set()

      status = {logger:self.check_logger(logger, manage, clear_errors)
                for logger in loggers}
      logging.debug('check_loggers got status: %s', pprint.pformat(status))
      return status

  ############################
  def run(self):
    """ Check up on loggers and discard status report."""
    try:
      while not self.quit_flag:
        status = self.check_loggers(manage=True, clear_errors=False)
        time.sleep(self.interval)

    except KeyboardInterrupt:
      logging.warning('Received KeyboardInterrupt. Exiting')
      self.quit()

  ############################
  def quit(self):
    """Exit the loop and shut down all loggers."""
    self.quit_flag = True

    # Set all loggers to "None" config, which should shut them down.
    # NOTE: because set_config(logger, None) deletes the logger in
    # question, we need to copy the keys into a list, otherwise we get
    # a "dictionary changed size during iteration" error.
    logging.info('Received quit request - shutting loggers down.')
    [logging.info('Shutting down logger %s', logger)
     for logger in list(self.logger_configs.keys())]
    [self.set_config(logger, None)
     for logger in list(self.logger_configs.keys())]
    for logger, proc in self.processes.items():
      if self.process_is_alive(proc):
        proc.kill()

################################################################################
if __name__ == '__main__':
  import argparse
  parser = argparse.ArgumentParser()
  parser.add_argument('--config', dest='config', action='store', default=None,
                      help='Initial set of configs to run.')

  parser.add_argument('--max_tries', dest='max_tries', action='store',
                      type=int, default=3, help='How many times to try a '
                      'crashing config before giving up on it as failed. If '
                      'zero, then never stop retrying.')
  parser.add_argument('--interval', dest='interval', action='store',
                      type=int, default=1,
                      help='How many seconds to sleep between logger checks.')

  parser.add_argument('--stderr_file', dest='stderr_file', default=None,
                      help='Optional file to which stderr messages should '
                      'be written.')
  parser.add_argument('-v', '--verbosity', dest='verbosity',
                      default=0, action='count',
                      help='Increase output verbosity')
  parser.add_argument('-V', '--logger_verbosity', dest='logger_verbosity',
                      default=0, action='count',
                      help='Increase output verbosity of component loggers')

  args = parser.parse_args()

  # Set up logging first of all
  LOG_LEVELS ={0:logging.WARNING, 1:logging.INFO, 2:logging.DEBUG}
  log_level = LOG_LEVELS[min(args.verbosity, max(LOG_LEVELS))]
  setUpStdErrLogging(log_level=log_level)
  if args.stderr_file:
    stderr_writers = [TextFileWriter(args.stderr_file)]
    logging.getLogger().addHandler(StdErrLoggingHandler(stderr_writers))

  # What level do we want our component loggers to write?
  logger_log_level = LOG_LEVELS[min(args.logger_verbosity, max(LOG_LEVELS))]

  initial_configs = read_config(args.config) if args.config else None

  runner = LoggerRunner(interval=args.interval, max_tries=args.max_tries,
                        initial_configs=initial_configs,
                        logger_log_level=logger_log_level)
  runner.run()
