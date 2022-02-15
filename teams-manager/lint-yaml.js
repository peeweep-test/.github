async function parseYaml() {
  // console.log("parseYaml");
  const yaml = require("js-yaml");
  const fs = require("fs");

  let teams;
  try {
    teams = yaml.load(fs.readFileSync("./teams.yaml", "utf-8")).teams;
    // console.log("%j", teams);

    return teams;
  } catch (e) {
    throw e;
  }
}

async function getOrgMembers(github, context) {
  try {
    const membersContext = await github.rest.orgs.listMembers({
      org: context.payload.organization.login,
    });
    var orgMembers = [];
    for (let memberContext of membersContext.data) {
      orgMembers.push(memberContext.login);
    }
    return orgMembers;
  } catch (e) {
    throw e;
  }
}

module.exports = async ({ github, context }) => {
  // parse yaml
  var teams = await parseYaml();
  // {"admin":{"members":["peeweep"],"repos":["all"]},"dtkcore":{"members":["peeweep"],"repos":["dtkcore"]}}
  console.log("yaml parse info: %j", teams);

  const orgMembers = await getOrgMembers(github, context);

  var memberNotInTeamList = [];
  for (let team in teams) {
    // 验证members 是否已在组织中

    for (member of teams[team].members) {
      if (orgMembers.indexOf(member) < 0) {
        // 如果成员不在组织中，将成员加入 memberNotInTeamList
        if (memberNotInTeamList.indexOf(member) < 0) {
          memberNotInTeamList.push(member);
        }
      }
    }
  }
  if (memberNotInTeamList.length > 0) {
    throw (
      "teams.yaml 校验失败: 请先将以下成员邀请进组织: " + memberNotInTeamList
    );
  }
};
