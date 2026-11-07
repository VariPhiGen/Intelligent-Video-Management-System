from . import webhook, telegram, slack, kafka, email, sftp
ADAPTERS={'webhook':webhook.send,'telegram':telegram.send,'slack':slack.send,'kafka':kafka.send,'email':email.send,'sftp':sftp.send}
